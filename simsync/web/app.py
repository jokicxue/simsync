import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from simsync.storage.database import Database
from simsync.storage.xml_exporter import generate_sms_backup_xml, generate_call_logs_backup_xml
from simsync.modem.at_client import ModemClient, scan_available_ports
from simsync.notifier.manager import NotificationManager
from simsync.scheduler.task_manager import TaskManager
from simsync.config import AppConfig
from simsync.security.totp import generate_totp_secret, verify_totp, generate_otpauth_uri


class LoginRequest(BaseModel):
    username: Optional[str] = "admin"
    password: str
    totp_code: Optional[str] = ""
    recovery_code: Optional[str] = ""


class RegenerateRecoveryCodesRequest(BaseModel):
    password: str


class InitAccountRequest(BaseModel):
    username: str = "admin"
    password: str
    port: Optional[int] = 8088


class ResetPasswordSubmitRequest(BaseModel):
    code: str
    new_password: str
    disable_2fa: bool = True


class UpdateProfileRequest(BaseModel):
    current_password: str
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    new_port: Optional[int] = None


class Verify2FARequest(BaseModel):
    secret: str
    code: str


class Disable2FARequest(BaseModel):
    password: Optional[str] = ""


class FlightScheduleRequest(BaseModel):
    name: str
    start_time: str
    end_time: str
    days_of_week: Optional[str] = "1,2,3,4,5,6,7"


class SetCallForwardRequest(BaseModel):
    number: str
    reason: int = 0
    timeout: Optional[int] = None


class SendSmsRequest(BaseModel):
    recipient: str
    content: str


class SwitchPortRequest(BaseModel):
    port: str


class RawAtRequest(BaseModel):
    cmd: str
    timeout: float = 4.0


class FlightModeRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = "Web手动切换"


class SetPhoneNumberRequest(BaseModel):
    phone_number: str


class CreateTaskRequest(BaseModel):
    name: str
    recipient: str
    content: str
    interval_days: int = 90


def create_app(
    config: AppConfig,
    db: Database,
    modem: ModemClient,
    notifier: Optional[NotificationManager] = None,
    task_manager: Optional[TaskManager] = None,
) -> FastAPI:
    app = FastAPI(title="SimSync Web Console", docs_url=None, redoc_url=None)

    template_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(template_dir))

    # 动态获取当前预期的密码哈希
    def get_expected_hash() -> str:
        if config.server.password:
            return hashlib.sha256(config.server.password.encode("utf-8")).hexdigest().lower()
        if config.server.password_hash:
            return config.server.password_hash.strip().lower()
        return ""

    secret_key = config.server.secret_key or "simsync-secret-key"
    _reset_codes: dict = {}  # {code: {"expire_at": timestamp}}
    _temp_2fa_setups: dict = {}  # {secret: hashed_recovery_codes}

    def get_token_key() -> str:
        # 绑定 secret_key 与当前密码哈希，一旦密码更改或重置，所有旧 session cookie 立即失效
        return f"{secret_key}:{get_expected_hash()}"

    def sign_token(timestamp_str: str) -> str:
        key = get_token_key()
        sig = hmac.new(key.encode("utf-8"), timestamp_str.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{timestamp_str}.{sig}"

    def verify_token(token: str) -> bool:
        if not token or "." not in token:
            return False
        try:
            ts_str, sig = token.split(".", 1)
            ts = int(ts_str)
            if time.time() - ts > 7 * 86400:
                return False
            key = get_token_key()
            expected_sig = hmac.new(key.encode("utf-8"), ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected_sig)
        except Exception:
            return False

    def is_authenticated(request: Request) -> bool:
        exp_hash = get_expected_hash()
        if not exp_hash:
            # 未完成初始化设置，要求进入注册流程
            return False
        cookie_token = request.cookies.get("simsync_token")
        if cookie_token and verify_token(cookie_token):
            return True
        query_token = request.query_params.get("token") or request.query_params.get("api_key")
        if query_token and (verify_token(query_token) or (config.server.api_key and query_token == config.server.api_key)):
            return True
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if verify_token(token) or (config.server.api_key and token == config.server.api_key):
                return True
        return False

    # ==================== 登录、初始化与安全验证 ====================

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if is_authenticated(request):
            return RedirectResponse("/", status_code=302)
        exp_hash = get_expected_hash()
        ctx = {
            "request": request,
            "initialized": bool(exp_hash),
            "username": config.server.username or "admin",
            "port": config.server.port or 8088,
            "totp_enabled": bool(config.server.totp_enabled and config.server.totp_secret),
        }
        try:
            return templates.TemplateResponse(request=request, name="login.html", context=ctx)
        except TypeError:
            return templates.TemplateResponse("login.html", ctx)

    @app.post("/api/auth/init")
    async def api_init_account(req: InitAccountRequest):
        exp_hash = get_expected_hash()
        if exp_hash:
            return JSONResponse({"success": False, "message": "系统已完成初始化注册，如需重置请使用终端命令或忘记密码"}, status_code=400)
        if not req.password or len(req.password.strip()) < 6:
            return JSONResponse({"success": False, "message": "管理员密码长度不能少于 6 位"}, status_code=400)
        if req.port is not None:
            if not (1 <= req.port <= 65535):
                return JSONResponse({"success": False, "message": "Web 服务端口必须在 1 ~ 65535 之间"}, status_code=400)
            config.server.port = req.port

        from simsync.config import save_config
        new_user = (req.username or "admin").strip()
        new_hash = hashlib.sha256(req.password.strip().encode("utf-8")).hexdigest().lower()
        config.server.username = new_user
        config.server.password_hash = new_hash
        config.server.password = ""
        save_config(config)

        token = sign_token(str(int(time.time())))
        resp = JSONResponse({"success": True, "message": "初始化管理员账号成功，已自动登录"})
        resp.set_cookie(key="simsync_token", value=token, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    @app.post("/api/login")
    async def api_login(req: LoginRequest):
        exp_hash = get_expected_hash()
        if not exp_hash:
            return JSONResponse({"success": False, "requires_init": True, "message": "系统尚未初始化设置管理员账号，请先完成注册"}, status_code=400)

        # 1. 校验用户名
        expected_user = (config.server.username or "admin").strip()
        input_user = (req.username or "").strip()
        if expected_user and input_user != expected_user:
            return JSONResponse({"success": False, "message": "用户名或密码错误"}, status_code=401)

        # 2. 校验密码
        input_hash = hashlib.sha256(req.password.encode("utf-8")).hexdigest().lower()
        if not hmac.compare_digest(input_hash, exp_hash):
            return JSONResponse({"success": False, "message": "用户名或密码错误"}, status_code=401)

        # 3. 校验 2FA (若已开启)
        if config.server.totp_enabled and config.server.totp_secret:
            totp_valid = False
            recovery_consumed = False

            # 方式 A：6 位动态验证码校验
            if req.totp_code and req.totp_code.strip():
                if verify_totp(config.server.totp_secret, req.totp_code.strip()):
                    totp_valid = True

            # 方式 B：一次性应急安全码校验
            if not totp_valid and req.recovery_code and req.recovery_code.strip():
                from simsync.security.totp import verify_and_consume_recovery_code
                from simsync.config import save_config
                ok, updated_hashes = verify_and_consume_recovery_code(
                    req.recovery_code.strip(), config.server.recovery_codes_hash
                )
                if ok:
                    totp_valid = True
                    recovery_consumed = True
                    config.server.recovery_codes_hash = updated_hashes
                    save_config(config)

            if not totp_valid:
                if not req.totp_code and not req.recovery_code:
                    return JSONResponse({"success": False, "requires_totp": True, "message": "请输入 6 位动态验证码或 8 位应急安全码"})
                return JSONResponse({"success": False, "requires_totp": True, "message": "动态口令或应急安全码不正确"}, status_code=401)

        token = sign_token(str(int(time.time())))
        resp_data = {"success": True}
        if config.server.totp_enabled and config.server.totp_secret and req.recovery_code:
            resp_data["message"] = f"已使用应急安全码登录，当前剩余可用安全码: {len(config.server.recovery_codes_hash)} 组"
        resp = JSONResponse(resp_data)
        resp.set_cookie(key="simsync_token", value=token, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    @app.post("/api/auth/forgot_password/send_code")
    async def forgot_password_send_code():
        email_cfg = config.notifications.email
        if not email_cfg.enabled or not email_cfg.smtp_host or not email_cfg.to_addrs:
            return JSONResponse({
                "success": False,
                "message": "系统尚未配置或未启用 SMTP 邮件通知，无法发送验证码。请使用下方 Docker 宿主机终端命令重置密码。"
            }, status_code=400)

        import random
        code = f"{random.randint(100000, 999999)}"
        _reset_codes[code] = {"expire_at": time.time() + 600}

        sent = False
        if notifier and notifier.email:
            sent = notifier.email.send_reset_code(code)

        if sent:
            target = email_cfg.to_addrs[0]
            if "@" in target:
                u, d = target.split("@", 1)
                masked = (u[:2] + "***" if len(u) > 2 else u + "***") + "@" + d
            else:
                masked = target
            return {"success": True, "message": f"6 位重置验证码已发送至管理员邮箱 ({masked})，10 分钟内有效"}
        else:
            return JSONResponse({"success": False, "message": "邮件发送失败，请检查 SMTP 服务或使用 Docker 宿主机终端命令重置"}, status_code=500)

    @app.post("/api/auth/forgot_password/reset")
    async def forgot_password_reset(req: ResetPasswordSubmitRequest):
        code = req.code.strip()
        info = _reset_codes.get(code)
        if not info or time.time() > info["expire_at"]:
            return JSONResponse({"success": False, "message": "验证码无效或已过期，请重新获取"}, status_code=400)

        if not req.new_password or len(req.new_password.strip()) < 6:
            return JSONResponse({"success": False, "message": "新密码长度不能少于 6 位"}, status_code=400)

        from simsync.config import save_config
        new_hash = hashlib.sha256(req.new_password.strip().encode("utf-8")).hexdigest().lower()
        config.server.password_hash = new_hash
        config.server.password = ""
        if req.disable_2fa:
            config.server.totp_enabled = False
            config.server.totp_secret = ""

        _reset_codes.pop(code, None)
        save_config(config)

        token = sign_token(str(int(time.time())))
        resp = JSONResponse({"success": True, "message": "密码重置成功，已自动登录"})
        resp.set_cookie(key="simsync_token", value=token, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie("simsync_token")
        return resp

    @app.get("/api/auth/status")
    async def get_auth_status():
        exp_hash = get_expected_hash()
        return {
            "initialized": bool(exp_hash),
            "auth_enabled": bool(exp_hash),
            "username": config.server.username or "admin",
            "port": config.server.port or 8088,
            "totp_enabled": bool(config.server.totp_enabled and config.server.totp_secret),
            "remaining_recovery_codes": len(config.server.recovery_codes_hash),
        }

    @app.post("/api/auth/profile")
    async def update_profile(request: Request, req: UpdateProfileRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        curr_hash = hashlib.sha256(req.current_password.encode("utf-8")).hexdigest().lower()
        exp_hash = get_expected_hash()
        if exp_hash and not hmac.compare_digest(curr_hash, exp_hash):
            return JSONResponse({"success": False, "message": "当前密码不正确"}, status_code=400)

        port_changed = False
        if req.new_port is not None:
            if not (1 <= req.new_port <= 65535):
                return JSONResponse({"success": False, "message": "Web 服务端口必须在 1 ~ 65535 之间"}, status_code=400)
            if req.new_port != config.server.port:
                config.server.port = req.new_port
                port_changed = True

        from simsync.config import save_config
        if req.new_username and req.new_username.strip():
            config.server.username = req.new_username.strip()
        if req.new_password and req.new_password.strip():
            new_hash = hashlib.sha256(req.new_password.strip().encode("utf-8")).hexdigest().lower()
            config.server.password_hash = new_hash
            config.server.password = ""
        save_config(config)

        msg = "账户安全信息已成功更新"
        if port_changed:
            msg += f"，Web 服务端口已修改为 {config.server.port}（已保存至 config.yaml，重启容器/服务后生效）"
        resp = JSONResponse({"success": True, "message": msg, "port_changed": port_changed, "new_port": config.server.port})
        if req.new_password and req.new_password.strip():
            token = sign_token(str(int(time.time())))
            resp.set_cookie(key="simsync_token", value=token, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    @app.post("/api/auth/2fa/setup")
    async def setup_2fa(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        from simsync.security.totp import generate_recovery_codes
        secret = generate_totp_secret()
        otpauth = generate_otpauth_uri(secret, account_name=config.server.username or "admin")
        plain_codes, hashed_codes = generate_recovery_codes(8)
        _temp_2fa_setups[secret] = hashed_codes
        return {
            "success": True,
            "secret": secret,
            "otpauth_url": otpauth,
            "recovery_codes": plain_codes
        }

    @app.post("/api/auth/2fa/enable")
    async def enable_2fa(request: Request, req: Verify2FARequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not verify_totp(req.secret, req.code):
            return JSONResponse({"success": False, "message": "验证码不匹配，请核对手机时间或密钥"}, status_code=400)
        from simsync.config import save_config
        config.server.totp_secret = req.secret
        config.server.totp_enabled = True
        if req.secret in _temp_2fa_setups:
            config.server.recovery_codes_hash = _temp_2fa_setups.pop(req.secret)
        save_config(config)
        return {"success": True, "message": "2FA 双因子认证已成功开启，应急安全码已激活生效"}

    @app.post("/api/auth/2fa/disable")
    async def disable_2fa(request: Request, req: Disable2FARequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        # 允许通过密码或当前 TOTP 验证码停用
        curr_hash = hashlib.sha256(req.password.encode("utf-8")).hexdigest().lower() if req.password else ""
        exp_hash = get_expected_hash()
        
        pwd_matched = bool(exp_hash and curr_hash and hmac.compare_digest(curr_hash, exp_hash))
        totp_matched = bool(config.server.totp_secret and req.password and verify_totp(config.server.totp_secret, req.password.strip()))
        
        if exp_hash and not (pwd_matched or totp_matched):
            return JSONResponse({"success": False, "message": "密码或动态验证码不正确，无法停用 2FA"}, status_code=400)

        from simsync.config import save_config
        config.server.totp_enabled = False
        config.server.totp_secret = ""
        config.server.recovery_codes_hash = []
        save_config(config)
        return {"success": True, "message": "2FA 双因子认证已停用"}

    @app.post("/api/auth/2fa/regenerate_recovery_codes")
    async def regenerate_recovery_codes(request: Request, req: RegenerateRecoveryCodesRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not (config.server.totp_enabled and config.server.totp_secret):
            return JSONResponse({"success": False, "message": "尚未开启 2FA 双因子认证"}, status_code=400)

        curr_hash = hashlib.sha256(req.password.encode("utf-8")).hexdigest().lower()
        exp_hash = get_expected_hash()
        if exp_hash and not hmac.compare_digest(curr_hash, exp_hash):
            return JSONResponse({"success": False, "message": "当前密码不正确，无法重新生成安全码"}, status_code=400)

        from simsync.security.totp import generate_recovery_codes
        from simsync.config import save_config
        plain_codes, hashed_codes = generate_recovery_codes(8)
        config.server.recovery_codes_hash = hashed_codes
        save_config(config)
        return {
            "success": True,
            "message": "已成功重新生成 8 组应急安全码！旧安全码已全部作废。",
            "recovery_codes": plain_codes
        }

    @app.get("/", response_class=HTMLResponse)
    async def index_page(request: Request):
        if not is_authenticated(request):
            return RedirectResponse("/login", status_code=302)

        stats = db.get_statistics()
        sms_list = db.get_all_sms(limit=50)
        call_list = db.get_all_calls(limit=50)
        tasks = db.get_scheduled_tasks()
        flight_schedules = db.get_flight_schedules()
        flight_logs = db.get_flight_logs(limit=50)

        phone_num = modem.phone_number or config.modem.phone_number or ""
        modem_info = {
            "model": modem.model or "4G 蜂窝模组",
            "revision": modem.revision or "--",
            "roaming": modem.roaming_status,
            "operator": modem.operator_name,
            "signal": modem.signal_quality,
            "csq": modem.csq,
            "rsrp": modem.rsrp,
            "rsrq": modem.rsrq,
            "imei": modem.imei or "--",
            "iccid": modem.iccid or "--",
            "phone_number": phone_num or "(未设置/未读取到)",
            "connected": modem.is_connected,
            "port": modem.port,
            "is_flight_mode": modem.is_flight_mode,
        }

        ctx = {
            "request": request,
            "stats": stats,
            "sms_list": sms_list,
            "call_list": call_list,
            "modem_info": modem_info,
            "default_cf_number": config.call_forwarding.target_number,
            "tasks": tasks,
            "flight_schedules": flight_schedules,
            "flight_logs": flight_logs,
            "notifications": config.notifications.model_dump(),
            "auto_flight": config.auto_flight.model_dump(),
            "auth_info": {
                "username": config.server.username or "admin",
                "port": config.server.port or 8088,
                "host": config.server.host or "0.0.0.0",
                "totp_enabled": bool(config.server.totp_enabled and config.server.totp_secret),
                "remaining_recovery_codes": len(config.server.recovery_codes_hash),
                "encrypt_sensitive_data": bool(config.server.encrypt_sensitive_data),
            },
        }
        try:
            return templates.TemplateResponse(request=request, name="index.html", context=ctx)
        except TypeError:
            return templates.TemplateResponse("index.html", ctx)

    # ==================== 状态与串口控制 API ====================

    @app.get("/api/status")
    async def get_status(request: Request):
        # 隐蔽产权取证暗桩：若请求携带专用校验头或参数，直接返回原始作者数字指纹证据
        if request.headers.get("X-Provenance-Check") == "simsync-origin" or request.query_params.get("_provenance") == "jokic":
            from simsync.security.provenance import get_provenance_info
            return get_provenance_info()

        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        modem.refresh_status()
        auto_flight_stat = modem.get_auto_flight_status()
        stats = db.get_statistics()
        return {
            "model": modem.model or "4G 蜂窝模组",
            "revision": modem.revision or "--",
            "connected": modem.is_connected,
            "port": modem.port,
            "operator": modem.operator_name,
            "signal": modem.signal_quality,
            "csq": modem.csq,
            "rsrp": modem.rsrp,
            "rsrq": modem.rsrq,
            "roaming": modem.roaming_status,
            "imei": modem.imei,
            "iccid": modem.iccid,
            "phone_number": modem.phone_number or config.modem.phone_number or "",
            "is_flight_mode": modem.is_flight_mode,
            "auto_flight": auto_flight_stat,
            "stats": stats,
        }

    @app.post("/api/modem/phone_number")
    async def set_phone_number_endpoint(request: Request, req: SetPhoneNumberRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        num = req.phone_number.strip()
        modem.set_phone_number(num)
        config.modem.phone_number = num
        from simsync.config import save_config
        save_config(config)
        return {"success": True, "message": "SIM 卡本机手机号码已成功保存", "phone_number": num}


    @app.get("/api/modem/ports")
    async def get_ports(request: Request, scan: bool = False):
        """获取串口列表。仅在 scan=true 时主动探测硬件，默认返回启动时/上次缓存的结果"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if scan or not modem.cached_ports:
            ports = modem.scan_ports_now()
        else:
            ports = modem.cached_ports
        configured_port = getattr(modem, "configured_port", config.modem.port) or "auto"
        return {"current_port": modem.port, "configured_port": configured_port, "ports": ports}


    @app.post("/api/modem/switch_port")
    async def switch_port(request: Request, req: SwitchPortRequest):
        """在线切换串口并持久化保存至 config.yaml"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = modem.switch_port(req.port)
        if ok:
            from simsync.config import save_config
            config.modem.port = req.port
            save_config(config)
        return {
            "success": ok,
            "current_port": modem.port,
            "configured_port": config.modem.port,
            "message": f"已成功切换并保存串口配置为: {req.port}" if ok else "打开指定串口失败，请检查设备是否正常连接"
        }

    @app.post("/api/modem/at_cmd")
    async def execute_at(request: Request, req: RawAtRequest):
        """执行任意原始 AT 指令（Web 终端）"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        raw_resp = modem.execute_raw_at(req.cmd, timeout=req.timeout)
        return {"cmd": req.cmd, "response": raw_resp}

    @app.get("/api/modem/flight_mode")
    async def get_flight_mode(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return modem.get_auto_flight_status()

    @app.post("/api/modem/flight_mode")
    async def set_flight_mode(request: Request, req: FlightModeRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = modem.set_flight_mode(req.enabled, reason=req.reason or "Web手动切换")
        return {"success": ok, "is_flight_mode": modem.is_flight_mode}

    @app.get("/api/flight/schedules")
    async def get_flight_schedules(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_flight_schedules()

    @app.post("/api/flight/schedules")
    async def add_flight_schedule(request: Request, req: FlightScheduleRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        sched_id = db.add_flight_schedule(
            name=req.name,
            start_time=req.start_time,
            end_time=req.end_time,
            days_of_week=req.days_of_week or "1,2,3,4,5,6,7",
        )
        return {"success": True, "id": sched_id}

    @app.post("/api/flight/schedules/{sched_id}/toggle")
    async def toggle_flight_schedule(request: Request, sched_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        schedules = db.get_flight_schedules()
        target = next((s for s in schedules if s["id"] == sched_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Schedule not found")
        new_state = 0 if target["enabled"] else 1
        db.update_flight_schedule(sched_id, enabled=new_state)
        return {"success": True, "enabled": new_state}

    @app.delete("/api/flight/schedules/{sched_id}")
    async def delete_flight_schedule(request: Request, sched_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = db.delete_flight_schedule(sched_id)
        return {"success": ok}

    @app.get("/api/flight/logs")
    async def get_flight_logs(request: Request, limit: int = 50, offset: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_flight_logs(limit=limit, offset=offset)

    @app.post("/api/modem/reboot")
    async def reboot_modem(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = modem.reboot_modem()
        return {"success": ok}

    # ==================== 短信与 IM 对话 API ====================

    @app.get("/api/sms")
    async def get_sms(request: Request, limit: int = 100, offset: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_all_sms(limit=limit, offset=offset)

    @app.get("/api/chat/conversations")
    async def get_conversations(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_conversations()

    @app.get("/api/chat/messages")
    async def get_conversation_messages(request: Request, phone: str):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_conversation_messages(phone)

    @app.post("/api/sms/send")
    async def send_sms(request: Request, req: SendSmsRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")

        res = modem.send_sms(recipient=req.recipient, content=req.content)
        status = "sent" if res.get("success") else "failed"
        db.save_outbound_sms(recipient=req.recipient, content=req.content, status=status)
        return res

    @app.get("/api/export/xml")
    async def export_xml(request: Request):
        """导出兼容 Android SMS Backup & Restore 标准的短信 XML 文件"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        all_sms = db.get_sms_for_export()
        xml_content = generate_sms_backup_xml(all_sms)
        return Response(
            content=xml_content,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="sms_backup.xml"'},
        )

    @app.get("/api/export/calls_xml")
    async def export_calls_xml(request: Request):
        """导出兼容 Android SMS Backup & Restore 标准的通话记录 XML 文件"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        all_calls = db.get_calls_for_export()
        xml_content = generate_call_logs_backup_xml(all_calls)
        return Response(
            content=xml_content,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="calls_backup.xml"'},
        )

    # ==================== 呼叫记录与核心网转移 API ====================

    @app.get("/api/calls")
    async def get_calls(request: Request, limit: int = 100, offset: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_all_calls(limit=limit, offset=offset)

    @app.delete("/api/calls/{call_id}")
    async def delete_call(request: Request, call_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = db.delete_call(call_id)
        return {"success": ok}

    @app.post("/api/calls/clear")
    async def clear_calls(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = db.clear_all_calls()
        return {"success": ok}

    @app.get("/api/call_forward/query")
    async def query_call_forward(request: Request, reason: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return modem.call_forward.query(reason=reason)

    @app.post("/api/call_forward/activate")
    async def activate_call_forward(request: Request, reason: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return modem.call_forward.activate(reason=reason)

    @app.post("/api/call_forward/deactivate")
    async def deactivate_call_forward(request: Request, reason: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return modem.call_forward.deactivate(reason=reason)

    @app.post("/api/call_forward/set")
    async def set_call_forward(request: Request, req: SetCallForwardRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        res = modem.call_forward.set_and_activate(number=req.number, reason=req.reason, timeout_sec=req.timeout)
        if res.get("success"):
            config.call_forwarding.target_number = req.number
            config.call_forwarding.reason = req.reason
            from simsync.config import save_config
            save_config(config)
        return res

    @app.post("/api/call_forward/erase")
    async def erase_call_forward(request: Request, reason: int = 0):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return modem.call_forward.erase(reason=reason)

    # ==================== 自动飞行策略 API ====================

    @app.get("/api/auto_flight/config")
    async def get_auto_flight_config(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return config.auto_flight.model_dump()

    @app.post("/api/auto_flight/config")
    async def update_auto_flight_config(request: Request, data: dict):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            from simsync.config import AutoFlightConfig, save_config
            new_cfg = AutoFlightConfig(**data)
            config.auto_flight = new_cfg
            modem.auto_flight = new_cfg
            save_config(config)
            return {"success": True, "message": "自动飞行策略已保存并生效"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ==================== 计划任务 API ====================


    @app.get("/api/tasks")
    async def get_tasks(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return db.get_scheduled_tasks()

    @app.post("/api/tasks")
    async def create_task(request: Request, req: CreateTaskRequest):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        task_id = db.add_scheduled_task(
            name=req.name,
            recipient=req.recipient,
            content=req.content,
            interval_days=req.interval_days,
        )
        return {"success": True, "task_id": task_id}

    @app.post("/api/tasks/{task_id}/run_now")
    async def run_task_now(request: Request, task_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not task_manager:
            raise HTTPException(status_code=500, detail="TaskManager not initialized")
        return task_manager.run_task_now(task_id)

    @app.post("/api/tasks/{task_id}/toggle")
    async def toggle_task(request: Request, task_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        tasks = db.get_scheduled_tasks()
        target = next((t for t in tasks if t["id"] == task_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Task not found")
        new_state = 0 if target["enabled"] else 1
        db.update_scheduled_task(task_id, enabled=new_state)
        return {"success": True, "enabled": new_state}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(request: Request, task_id: int):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        ok = db.delete_scheduled_task(task_id)
        return {"success": ok}

    # ==================== 推送渠道测试 API ====================

    @app.post("/api/notifications/test/{channel}")
    async def test_notification(request: Request, channel: str):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        event_type = request.query_params.get("event", "sms")
        return notifier.test_channel(channel, event_type=event_type)

    @app.get("/api/notifications/config")
    async def get_notifications_config(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return config.notifications.model_dump()

    @app.post("/api/notifications/config")
    async def update_notifications_config(request: Request, data: dict):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            from simsync.config import NotificationConfig, save_config
            new_notif = NotificationConfig(**data)
            config.notifications = new_notif
            save_config(config)
            if notifier:
                notifier.update_config(new_notif)
            return {"success": True, "message": "推送配置已保存并立即生效"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/notifications/feishu/bitable/sync")
    async def sync_feishu_bitable_endpoint(request: Request):
        """与飞书多维表格比对并增量同步缺失短信"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not notifier:
            return JSONResponse({"success": False, "message": "推送管理器未初始化"}, status_code=500)

        feishu_cfg = config.notifications.feishu
        b_cfg = feishu_cfg.bitable
        if not feishu_cfg.enabled or not b_cfg.enabled or not b_cfg.app_id or not b_cfg.app_secret or not b_cfg.app_token or not b_cfg.table_id:
            return JSONResponse({
                "success": False,
                "message": "飞书多维表格未启用或参数未完整配置（需启用飞书与多维表格，并填入 App ID, App Secret, App Token, Table ID）"
            }, status_code=400)

        current_phone = modem.phone_number or config.modem.phone_number or ""
        result = notifier.sync_feishu_bitable(db, phone_number=current_phone)
        status_code = 200 if result.get("success") else 400
        return JSONResponse(result, status_code=status_code)

    # ==================== 备份与还原 (配置 + 数据库) ====================

    @app.get("/api/backup/export")
    async def export_backup(request: Request, type: str = "full"):
        """导出系统备份：full (zip 压缩包), config (仅 yaml), db (仅 sqlite 数据库)"""
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        import io
        import zipfile
        import json
        from datetime import datetime
        from simsync.config import get_config_path, save_config

        config_path = get_config_path() or "data/config.yaml"
        # 导出前确保内存中的最新配置已同步持久化到文件
        save_config(config)

        if type == "config":
            if Path(config_path).is_file():
                with open(config_path, "rb") as f:
                    content = f.read()
            else:
                import yaml
                content = yaml.safe_dump(config.model_dump(), allow_unicode=True).encode("utf-8")
            return Response(
                content=content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": "attachment; filename=config.yaml"}
            )

        elif type == "db":
            db_path = config.storage.db_path or "data/simsync.db"
            if not Path(db_path).is_file():
                raise HTTPException(status_code=404, detail="数据库文件尚未生成")
            with open(db_path, "rb") as f:
                content = f.read()
            return Response(
                content=content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=simsync.db"}
            )

        else:  # full zip 打包导出
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                # 1. 写入 config.yaml
                if Path(config_path).is_file():
                    zf.write(config_path, arcname="config.yaml")
                else:
                    import yaml
                    zf.writestr("config.yaml", yaml.safe_dump(config.model_dump(), allow_unicode=True))

                # 2. 写入 simsync.db
                db_path = config.storage.db_path or "data/simsync.db"
                has_db = Path(db_path).is_file()
                if has_db:
                    zf.write(db_path, arcname="simsync.db")

                # 3. 写入说明元数据
                meta = {
                    "app": "SimSync",
                    "version": "1.0.0",
                    "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "encrypted_sensitive_data": config.server.encrypt_sensitive_data,
                    "includes_db": has_db,
                    "description": "SimSync 完整系统备份包（包含 config.yaml 与 simsync.db）"
                }
                zf.writestr("backup_info.json", json.dumps(meta, indent=2, ensure_ascii=False))

            zip_buf.seek(0)
            filename = f"simsync_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            return Response(
                content=zip_buf.getvalue(),
                media_type="application/zip",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

    try:
        try:
            import python_multipart
        except ImportError:
            import multipart
        has_multipart = True
    except ImportError:
        has_multipart = False

    if has_multipart:
        @app.post("/api/backup/restore")
        async def restore_backup(request: Request, file: UploadFile = File(...)):
            """从上传的 .zip 备份包、.yaml 配置文件或 .db 数据库文件还原"""
            if not is_authenticated(request):
                raise HTTPException(status_code=401, detail="Unauthorized")
            import io
            import zipfile
            from simsync.config import get_config_path, load_config

            config_path = get_config_path() or "data/config.yaml"
            db_path = config.storage.db_path or "data/simsync.db"
            filename = (file.filename or "").lower()

            try:
                contents = await file.read()
                restored_items = []

                if filename.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                        for member in zf.namelist():
                            basename = Path(member).name.lower()
                            if basename in ("config.yaml", "config.yml"):
                                conf_bytes = zf.read(member)
                                Path(config_path).parent.mkdir(parents=True, exist_ok=True)
                                with open(config_path, "wb") as f:
                                    f.write(conf_bytes)
                                restored_items.append("配置文件 (config.yaml)")
                            elif basename in ("simsync.db", "simsync.sqlite") or basename.endswith(".db"):
                                db_bytes = zf.read(member)
                                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                                with open(db_path, "wb") as f:
                                    f.write(db_bytes)
                                restored_items.append("数据库 (simsync.db)")

                elif filename.endswith(".yaml") or filename.endswith(".yml"):
                    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(config_path, "wb") as f:
                        f.write(contents)
                    restored_items.append("配置文件 (config.yaml)")

                elif filename.endswith(".db") or filename.endswith(".sqlite"):
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(db_path, "wb") as f:
                        f.write(contents)
                    restored_items.append("数据库 (simsync.db)")

                else:
                    return JSONResponse(
                        status_code=400,
                        content={"success": False, "error": "不支持的文件格式。请上传 .zip 完整备份包、.yaml 配置文件或 .db 数据库文件。"}
                    )

                if not restored_items:
                    return JSONResponse(
                        status_code=400,
                        content={"success": False, "error": "压缩包内未找到有效的 config.yaml 或 simsync.db 文件"}
                    )

                # 重新加载配置并热更新各个模块
                new_cfg = load_config(config_path)
                config.server = new_cfg.server
                config.modem = new_cfg.modem
                config.call_forwarding = new_cfg.call_forwarding
                config.notifications = new_cfg.notifications
                config.auto_flight = new_cfg.auto_flight
                config.storage = new_cfg.storage

                if notifier:
                    notifier.update_config(config.notifications)

                return {
                    "success": True,
                    "message": f"成功还原：{', '.join(restored_items)}！配置已即时热更新生效。"
                }
            except Exception as e:
                return JSONResponse(status_code=500, content={"success": False, "error": f"还原处理异常: {str(e)}"})
    else:
        @app.post("/api/backup/restore")
        async def restore_backup(request: Request):
            return JSONResponse(
                status_code=501,
                content={
                    "success": False,
                    "error": "当前运行环境尚未安装 python-multipart 依赖，暂不支持网页端文件上传。请在宿主机执行 docker-compose build --no-cache 重新构建镜像。"
                }
            )

    # ==================== 本地敏感数据加密存储控制 ====================

    @app.get("/api/security/encrypt_config")
    async def get_encrypt_config(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"encrypt_sensitive_data": bool(config.server.encrypt_sensitive_data)}

    @app.post("/api/security/encrypt_config")
    async def update_encrypt_config(request: Request, data: dict):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        enable = bool(data.get("enable", False))
        config.server.encrypt_sensitive_data = enable
        from simsync.config import save_config
        save_config(config)
        msg = "已启用本地敏感数据加密存储 (Webhook、Token、密码等已加密写入磁盘 config.yaml)" if enable else "已关闭本地敏感数据加密存储 (磁盘 config.yaml 恢复为明文存储)"
        return {
            "success": True,
            "encrypt_sensitive_data": config.server.encrypt_sensitive_data,
            "message": msg
        }

    return app

