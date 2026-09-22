import os
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8088
    username: str = "admin"
    api_key: Optional[str] = ""
    # 外网访问密码（可填明文，也可填 SHA-256 哈希值；留空时首次访问进入设置向导）
    password: Optional[str] = ""
    password_hash: Optional[str] = ""
    # 2FA 双因子认证 (RFC 6238 TOTP)
    totp_enabled: bool = False
    totp_secret: Optional[str] = ""
    # 2FA 应急安全恢复码 (存储 SHA-256 哈希列表)
    recovery_codes_hash: List[str] = Field(default_factory=list)
    # Session 密钥 (留空自动生成)
    secret_key: Optional[str] = "simsync-secret-key-change-it"
    # 是否启用敏感数据本地加密存储 (加密 Webhook, Token, 密码等)
    encrypt_sensitive_data: bool = False


class ModemConfig(BaseModel):
    # 串口设备路径："auto" (自动探测首个响应 AT 的设备) 或指定路径如 /dev/ttyACM0, /dev/ttyUSB1, COM3
    port: str = "auto"
    baudrate: int = 115200
    timeout: int = 5
    auto_hangup_calls: bool = True
    # 本机手机号码（若 SIM 卡 EF_MSISDN 未写入或需手动指定）
    phone_number: Optional[str] = ""


class CallForwardingConfig(BaseModel):
    target_number: str = ""
    reason: int = 0  # 0=unconditional, 1=busy, 2=no reply, 3=not reachable


class FeishuBitableConfig(BaseModel):
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    app_token: str = ""
    table_id: str = ""
    # 来电多维表格归档
    call_enabled: bool = False
    call_mode: str = "same_base"  # "same_base" (同一个文档不同工作表), "same_table" (同一个工作表), "custom" (完全独立文档)
    call_app_token: str = ""
    call_table_id: str = ""


class FeishuConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_webhook_url: str = ""
    bitable: FeishuBitableConfig = Field(default_factory=FeishuBitableConfig)


class WecomBotConfig(BaseModel):
    webhook_url: str = ""


class WxpusherConfig(BaseModel):
    app_token: str = ""
    uids: List[str] = Field(default_factory=list)


class WechatConfig(BaseModel):
    enabled: bool = False
    mode: str = "wecom_bot"  # wecom_bot or wxpusher
    wecom_bot: WecomBotConfig = Field(default_factory=WecomBotConfig)
    wxpusher: WxpusherConfig = Field(default_factory=WxpusherConfig)
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_wecom_webhook_url: str = ""
    call_wxpusher_uids: List[str] = Field(default_factory=list)


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: List[str] = Field(default_factory=list)
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_to_addrs: List[str] = Field(default_factory=list)


class DingtalkConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    secret: str = ""
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_webhook_url: str = ""
    call_secret: str = ""


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    proxy: str = ""
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_chat_id: str = ""


class CustomWebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    method: str = "POST"
    # 来电提醒
    call_enabled: bool = True
    call_use_custom: bool = False
    call_url: str = ""
    call_method: str = "POST"


class NotificationConfig(BaseModel):
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    wechat: WechatConfig = Field(default_factory=WechatConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    dingtalk: DingtalkConfig = Field(default_factory=DingtalkConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    webhook: CustomWebhookConfig = Field(default_factory=CustomWebhookConfig)


class AutoFlightConfig(BaseModel):
    enabled: bool = False
    auto_flight_on_received: bool = False
    delay_minutes_on_received: int = 5
    auto_flight_on_sent: bool = False
    delay_minutes_on_sent: int = 5
    wake_before_send: bool = True


class StorageConfig(BaseModel):
    db_path: str = "data/simsync.db"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    modem: ModemConfig = Field(default_factory=ModemConfig)
    call_forwarding: CallForwardingConfig = Field(default_factory=CallForwardingConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    auto_flight: AutoFlightConfig = Field(default_factory=AutoFlightConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


_CONFIG_FILE_PATH: Optional[str] = None


def get_config_path() -> Optional[str]:
    return _CONFIG_FILE_PATH


def _backup_config_to_sqlite(cfg: AppConfig, db_path: Optional[str] = None):
    """将 AppConfig 完整配置以 JSON 镜像保存至 SQLite system_meta 表，并确保全量数据库表初始化"""
    import json
    import sqlite3
    target_db = db_path or cfg.storage.db_path or "data/simsync.db"
    try:
        from simsync.storage.database import Database
        # 确保完整数据库结构已被初始化（包括 sms_messages, call_logs 等）
        db = Database(target_db)
        db.save_config_backup(cfg.model_dump())
    except Exception:
        # 回退保险：直接写入 SQLite
        try:
            Path(target_db).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            backup_json = json.dumps(cfg.model_dump(), ensure_ascii=False)
            cursor.execute(
                """
                INSERT INTO system_meta (key, value, updated_at)
                VALUES ('backup_config_json', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (backup_json,),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def _restore_config_from_sqlite(db_path: Optional[str] = None) -> Optional[dict]:
    """从 SQLite system_meta 表查找并读取配置镜像备份"""
    import json
    import sqlite3
    paths_to_check = [
        db_path,
        "data/simsync.db",
        "/app/data/simsync.db",
    ]
    for p in paths_to_check:
        if p and Path(p).is_file():
            try:
                conn = sqlite3.connect(p)
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM system_meta WHERE key = 'backup_config_json'")
                row = cursor.fetchone()
                conn.close()
                if row and row[0]:
                    return json.loads(row[0])
            except Exception:
                pass
    return None


def _is_config_empty_or_default(cfg: AppConfig) -> bool:
    """判断配置是否处于初始空模板状态（例如未配置密码且未开启通知/手机号）"""
    has_pwd = bool((cfg.server.password or "").strip() or (cfg.server.password_hash or "").strip())
    notifs = cfg.notifications
    has_active_notif = any([
        notifs.feishu.enabled,
        notifs.wechat.enabled,
        notifs.email.enabled,
        notifs.dingtalk.enabled,
        notifs.telegram.enabled,
        notifs.webhook.enabled,
    ])
    has_phone = bool((cfg.modem.phone_number or "").strip())
    return (not has_pwd) and (not has_active_notif) and (not has_phone)


def save_config(cfg: AppConfig, target_path: Optional[str] = None) -> bool:
    """将 AppConfig 配置写回 yaml 配置文件，并同步双重备份到 SQLite 数据库"""
    global _CONFIG_FILE_PATH
    path = target_path or _CONFIG_FILE_PATH or "data/config.yaml"
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = cfg.model_dump()
        
        # 敏感信息本地加密存储逻辑
        if cfg.server.encrypt_sensitive_data:
            from simsync.security.crypto import encrypt_dict_sensitive_fields
            data = encrypt_dict_sensitive_fields(data, cfg.server.secret_key or "simsync-secret-key-change-it")

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

        # 同步双重备份到 SQLite system_meta
        _backup_config_to_sqlite(cfg)
        return True
    except Exception:
        return False


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """加载配置文件，若包含明文密码则自动加密并安全回写；若检测到为初始空配置则从 SQLite 自动恢复"""
    global _CONFIG_FILE_PATH
    import hashlib
    import re
    import logging

    log = logging.getLogger("simsync.config")

    paths_to_check = [
        config_path,
        os.environ.get("SIMSYNC_CONFIG"),
        "config.yaml",
        "data/config.yaml",
        "/app/data/config.yaml",
        "/etc/simsync/config.yaml",
        "config.example.yaml",
    ]

    selected_path = None
    for p in paths_to_check:
        if p and Path(p).is_file():
            selected_path = p
            break

    if not selected_path:
        # 如果没有任何配置文件，尝试从 SQLite 恢复
        backup = _restore_config_from_sqlite()
        if backup:
            cfg = AppConfig(**backup)
            save_config(cfg, "data/config.yaml")
            return cfg
        return AppConfig()

    _CONFIG_FILE_PATH = selected_path

    with open(selected_path, "r", encoding="utf-8") as f:
        content_text = f.read()
        data = yaml.safe_load(content_text) or {}

    # 若包含 enc: 加密字段，自动在内存中解密供程序使用
    try:
        from simsync.security.crypto import decrypt_dict_sensitive_fields
        secret = data.get("server", {}).get("secret_key", "simsync-secret-key-change-it")
        data = decrypt_dict_sensitive_fields(data, secret)
    except Exception:
        pass

    cfg = AppConfig(**data)

    # 自愈恢复：若检测到为初始默认配置（通常发生于代码更新覆盖 config.yaml），从 SQLite 恢复
    if _is_config_empty_or_default(cfg):
        backup = _restore_config_from_sqlite(cfg.storage.db_path)
        if backup:
            backup_cfg = AppConfig(**backup)
            if not _is_config_empty_or_default(backup_cfg):
                cfg = backup_cfg
                log.info("检测到配置文件处于初始状态，已成功从 SQLite system_meta 恢复账号密码与推送配置并回写 config.yaml")
                save_config(cfg, selected_path)
                return cfg

    # 自动加密逻辑：如果用户填写了明文 password，自动计算 SHA-256 哈希并回写文件
    raw_pwd = cfg.server.password
    if raw_pwd:
        pwd_hash = hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest().lower()
        cfg.server.password_hash = pwd_hash
        cfg.server.password = ""

        # 尝试安全回写配置文件，将明文 password 替换为 password_hash
        try:
            # 替换 yaml 文件中的 password 行
            new_text = re.sub(
                r'^[ \t]*password:[ \t]*["\']?.*?["\']?[ \t]*$',
                f'  # 密码已自动加密存储\n  password_hash: "{pwd_hash}"',
                content_text,
                flags=re.MULTILINE,
            )
            with open(selected_path, "w", encoding="utf-8") as f:
                f.write(new_text)
            _backup_config_to_sqlite(cfg)
        except Exception:
            pass

    return cfg

