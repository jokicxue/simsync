import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

import simsync.config
from simsync.config import AppConfig
from simsync.storage.database import Database
from simsync.security.totp import (
    generate_totp_secret,
    get_totp_token,
    verify_totp,
    generate_otpauth_uri,
)
from simsync.modem.at_client import ModemClient
from simsync.web.app import create_app


class TestAuthAndFlight(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_cfg_path = os.path.join(self.test_dir, "test_config.yaml")
        simsync.config._CONFIG_FILE_PATH = self.test_cfg_path

        self.db_path = os.path.join(self.test_dir, "test_auth_flight.db")
        self.db = Database(self.db_path)

        self.config = AppConfig()
        self.config.server.username = "admin"
        self.config.server.password = "MySecurePassword123"
        self.config.storage.db_path = self.db_path

        self.modem = ModemClient(port="COM_NONE")
        self.notifier = MagicMock()
        self.notifier.email = MagicMock()

        self.app = create_app(self.config, self.db, self.modem, notifier=self.notifier)
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_totp_generation_and_verification(self):
        secret = generate_totp_secret()
        self.assertGreaterEqual(len(secret), 26)

        token = get_totp_token(secret)
        self.assertEqual(len(token), 6)
        self.assertTrue(token.isdigit())

        self.assertTrue(verify_totp(secret, token))
        self.assertFalse(verify_totp(secret, "000000" if token != "000000" else "111111"))
        self.assertFalse(verify_totp(secret, "abc"))
        self.assertFalse(verify_totp(secret, ""))

        uri = generate_otpauth_uri(secret, "admin", "SimSync")
        self.assertTrue(uri.startswith("otpauth://totp/SimSync:admin?secret="))
        self.assertIn("issuer=SimSync", uri)

    def test_flight_schedules_and_logs(self):
        sched_id = self.db.add_flight_schedule(
            name="夜间免打扰",
            start_time="23:00",
            end_time="07:00",
            days_of_week="1,2,3,4,5",
        )
        self.assertGreater(sched_id, 0)

        schedules = self.db.get_flight_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["name"], "夜间免打扰")
        self.assertEqual(schedules[0]["start_time"], "23:00")
        self.assertEqual(schedules[0]["end_time"], "07:00")
        self.assertEqual(schedules[0]["enabled"], 1)

        self.db.update_flight_schedule(sched_id, enabled=False)
        updated = self.db.get_flight_schedules()
        self.assertEqual(updated[0]["enabled"], 0)

        self.db.save_flight_log(
            event="enter",
            reason="计划任务触发",
        )
        logs = self.db.get_flight_logs(limit=10)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event"], "enter")
        self.assertEqual(logs[0]["reason"], "计划任务触发")

        self.db.delete_flight_schedule(sched_id)
        self.assertEqual(len(self.db.get_flight_schedules()), 0)

    def test_call_history_database_operations(self):
        call_id = self.db.save_call(
            caller="+61412345678",
            action="auto_hangup",
        )
        self.assertGreater(call_id, 0)

        calls = self.db.get_all_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["caller"], "+61412345678")

        self.db.delete_call(call_id)
        self.assertEqual(len(self.db.get_all_calls()), 0)

    def test_init_account_flow(self):
        # 1. 建立一个未初始化的新环境
        uninit_cfg = AppConfig()
        uninit_cfg.storage.db_path = self.db_path
        uninit_cfg.server.password = ""
        uninit_cfg.server.password_hash = ""
        uninit_app = create_app(uninit_cfg, self.db, self.modem)
        uninit_client = TestClient(uninit_app)

        # a. 查询状态为未初始化
        status_resp = uninit_client.get("/api/auth/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertFalse(status_resp.json()["initialized"])

        # b. 尝试直接登录被拦截
        login_resp = uninit_client.post("/api/login", json={"username": "admin", "password": "123"})
        self.assertEqual(login_resp.status_code, 400)
        self.assertTrue(login_resp.json().get("requires_init"))

        # c. 密码少于 6 位报错
        fail_init = uninit_client.post("/api/auth/init", json={"username": "admin", "password": "123"})
        self.assertEqual(fail_init.status_code, 400)

        # d. 无效端口报错
        fail_port = uninit_client.post("/api/auth/init", json={"username": "admin", "password": "ValidPassword123", "port": 70000})
        self.assertEqual(fail_port.status_code, 400)

        # e. 成功完成初始化注册并指定自定义端口
        ok_init = uninit_client.post("/api/auth/init", json={"username": "admin", "password": "NewSecretPassword888", "port": 8099})
        self.assertEqual(ok_init.status_code, 200)
        self.assertTrue(ok_init.json()["success"])
        self.assertIn("simsync_token", ok_init.cookies)
        self.assertEqual(uninit_cfg.server.username, "admin")
        self.assertEqual(uninit_cfg.server.port, 8099)
        self.assertTrue(len(uninit_cfg.server.password_hash) > 20)

        # f. 已初始化后再次尝试初始化报错
        dup_init = uninit_client.post("/api/auth/init", json={"username": "admin", "password": "AnotherPassword"})
        self.assertEqual(dup_init.status_code, 400)

    def test_web_auth_login_and_2fa_flow(self):
        # 1. 错误密码登录失败
        resp = self.client.post("/api/login", json={"username": "admin", "password": "WrongPassword"})
        self.assertEqual(resp.status_code, 401)

        # 2. 错误用户名登录失败
        resp = self.client.post("/api/login", json={"username": "hacker", "password": "MySecurePassword123"})
        self.assertEqual(resp.status_code, 401)

        # 3. 正确凭据登录成功并获取 Cookie
        resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("success"))
        self.assertIn("simsync_token", resp.cookies)
        auth_cookie = resp.cookies

        # 4. 开启 2FA 流程
        setup_resp = self.client.post("/api/auth/2fa/setup", cookies=auth_cookie)
        self.assertEqual(setup_resp.status_code, 200)
        secret = setup_resp.json()["secret"]

        token = get_totp_token(secret)
        ok_enable = self.client.post(
            "/api/auth/2fa/enable",
            json={"secret": secret, "code": token},
            cookies=auth_cookie,
        )
        self.assertEqual(ok_enable.status_code, 200)
        self.assertTrue(self.config.server.totp_enabled)

        # 5. 开启 2FA 后的登录测试
        # 不带 2FA 登录 -> 提示 requires_totp
        no_2fa_resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertTrue(no_2fa_resp.json().get("requires_totp"))

        # 携带正确 2FA 登录成功
        token2 = get_totp_token(secret)
        full_resp = self.client.post(
            "/api/login",
            json={"username": "admin", "password": "MySecurePassword123", "totp_code": token2},
        )
        self.assertEqual(full_resp.status_code, 200)
        self.assertTrue(full_resp.json().get("success"))

        # 6. 关闭 2FA 测试 (使用动态口令关闭)
        token3 = get_totp_token(secret)
        disable_resp = self.client.post(
            "/api/auth/2fa/disable",
            json={"password": token3},
            cookies=auth_cookie,
        )
        self.assertEqual(disable_resp.status_code, 200)
        self.assertFalse(self.config.server.totp_enabled)

        # 7. 重新开启 2FA 后，使用账户密码关闭
        self.client.post("/api/auth/2fa/enable", json={"secret": secret, "code": get_totp_token(secret)}, cookies=auth_cookie)
        self.assertTrue(self.config.server.totp_enabled)

        disable_by_pwd = self.client.post(
            "/api/auth/2fa/disable",
            json={"password": "MySecurePassword123"},
            cookies=auth_cookie,
        )
        self.assertEqual(disable_by_pwd.status_code, 200)
        self.assertFalse(self.config.server.totp_enabled)

    def test_forgot_password_flow(self):
        # 1. 尚未配置邮箱时请求发送验证码 -> 提示无法发送
        resp_no_mail = self.client.post("/api/auth/forgot_password/send_code")
        self.assertEqual(resp_no_mail.status_code, 400)

        # 2. 配置 SMTP 邮箱后请求发送验证码
        self.config.notifications.email.enabled = True
        self.config.notifications.email.smtp_host = "smtp.example.com"
        self.config.notifications.email.to_addrs = ["admin@example.com"]
        self.notifier.email.send_reset_code.return_value = True

        resp_send = self.client.post("/api/auth/forgot_password/send_code")
        self.assertEqual(resp_send.status_code, 200)
        self.assertTrue(resp_send.json()["success"])
        self.notifier.email.send_reset_code.assert_called_once()
        sent_code = self.notifier.email.send_reset_code.call_args[0][0]
        self.assertEqual(len(sent_code), 6)

        # 3. 错误验证码重置失败
        fail_reset = self.client.post("/api/auth/forgot_password/reset", json={
            "code": "000000" if sent_code != "000000" else "111111",
            "new_password": "BrandNewPassword123",
            "disable_2fa": True,
        })
        self.assertEqual(fail_reset.status_code, 400)

        # 4. 正确验证码重置成功
        ok_reset = self.client.post("/api/auth/forgot_password/reset", json={
            "code": sent_code,
            "new_password": "BrandNewPassword123",
            "disable_2fa": True,
        })
        self.assertEqual(ok_reset.status_code, 200)
        self.assertTrue(ok_reset.json()["success"])
        self.assertIn("simsync_token", ok_reset.cookies)

        # 5. 使用新密码能够成功登录
        login_new = self.client.post("/api/login", json={
            "username": "admin",
            "password": "BrandNewPassword123"
        })
        self.assertEqual(login_new.status_code, 200)
        self.assertTrue(login_new.json()["success"])

    def test_update_profile_and_port(self):
        # 1. 登录
        resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(resp.status_code, 200)
        cookies = resp.cookies

        # 2. 错误密码无法修改端口
        bad_pwd = self.client.post("/api/auth/profile", json={
            "current_password": "WrongPassword",
            "new_port": 8888
        }, cookies=cookies)
        self.assertEqual(bad_pwd.status_code, 400)

        # 3. 越界端口报错
        bad_port = self.client.post("/api/auth/profile", json={
            "current_password": "MySecurePassword123",
            "new_port": 70000
        }, cookies=cookies)
        self.assertEqual(bad_port.status_code, 400)

        # 4. 正确更新端口
        ok_port = self.client.post("/api/auth/profile", json={
            "current_password": "MySecurePassword123",
            "new_port": 8888
        }, cookies=cookies)
        self.assertEqual(ok_port.status_code, 200)
        self.assertTrue(ok_port.json()["success"])
        self.assertEqual(self.config.server.port, 8888)

    def test_cli_reset_port(self):
        from simsync.security.reset import reset_credentials
        from simsync.config import load_config
        # 使用 CLI 重置工具修改端口
        reset_credentials(port=9090, config_path=self.test_cfg_path)
        reloaded = load_config(self.test_cfg_path)
        self.assertEqual(reloaded.server.port, 9090)

    def test_2fa_recovery_codes_flow(self):
        # 1. 登录
        resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(resp.status_code, 200)
        auth_cookie = resp.cookies

        # 2. 2FA setup 生成 8 组应急安全码
        setup_resp = self.client.post("/api/auth/2fa/setup", cookies=auth_cookie)
        self.assertEqual(setup_resp.status_code, 200)
        data = setup_resp.json()
        secret = data["secret"]
        recovery_codes = data.get("recovery_codes", [])
        self.assertEqual(len(recovery_codes), 8)
        first_code = recovery_codes[0]

        # 3. 启用 2FA
        enable_resp = self.client.post(
            "/api/auth/2fa/enable",
            json={"secret": secret, "code": get_totp_token(secret)},
            cookies=auth_cookie,
        )
        self.assertEqual(enable_resp.status_code, 200)
        self.assertTrue(self.config.server.totp_enabled)
        self.assertEqual(len(self.config.server.recovery_codes_hash), 8)

        # 4. 使用应急安全码登录
        login_with_code = self.client.post("/api/login", json={
            "username": "admin",
            "password": "MySecurePassword123",
            "recovery_code": first_code
        })
        self.assertEqual(login_with_code.status_code, 200)
        self.assertTrue(login_with_code.json().get("success"))
        # 验证该安全码已被核销消耗
        self.assertEqual(len(self.config.server.recovery_codes_hash), 7)

        # 5. 再次使用已核销的安全码登录 -> 失败
        reuse_code = self.client.post("/api/login", json={
            "username": "admin",
            "password": "MySecurePassword123",
            "recovery_code": first_code
        })
        self.assertEqual(reuse_code.status_code, 401)

        # 6. 重新生成 8 组安全码
        # 密码错误 -> 拒绝
        regen_bad = self.client.post("/api/auth/2fa/regenerate_recovery_codes", json={"password": "wrong"}, cookies=auth_cookie)
        self.assertEqual(regen_bad.status_code, 400)

        # 密码正确 -> 成功并返回 8 组新码
        regen_ok = self.client.post("/api/auth/2fa/regenerate_recovery_codes", json={"password": "MySecurePassword123"}, cookies=auth_cookie)
        self.assertEqual(regen_ok.status_code, 200)
        new_codes = regen_ok.json().get("recovery_codes", [])
        self.assertEqual(len(new_codes), 8)
        self.assertEqual(len(self.config.server.recovery_codes_hash), 8)

        # 7. 使用新安全码登录
        login_new_code = self.client.post("/api/login", json={
            "username": "admin",
            "password": "MySecurePassword123",
            "recovery_code": new_codes[0]
        })
        self.assertEqual(login_new_code.status_code, 200)
        self.assertTrue(login_new_code.json().get("success"))
        self.assertEqual(len(self.config.server.recovery_codes_hash), 7)

        # 8. 关闭 2FA -> 清空安全码哈希
        disable_resp = self.client.post(
            "/api/auth/2fa/disable",
            json={"password": "MySecurePassword123"},
            cookies=auth_cookie,
        )
        self.assertEqual(disable_resp.status_code, 200)
        self.assertFalse(self.config.server.totp_enabled)
        self.assertEqual(len(self.config.server.recovery_codes_hash), 0)


if __name__ == "__main__":
    unittest.main()
