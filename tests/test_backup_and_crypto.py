import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from fastapi.testclient import TestClient

import simsync.config
from simsync.config import AppConfig, save_config, load_config
from simsync.storage.database import Database
from simsync.notifier.manager import NotificationManager
from simsync.modem.at_client import ModemClient
from simsync.web.app import create_app
from simsync.security.crypto import (
    encrypt_value,
    decrypt_value,
    encrypt_dict_sensitive_fields,
    decrypt_dict_sensitive_fields,
)


class TestBackupAndCrypto(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_cfg_path = os.path.join(self.test_dir, "test_config.yaml")
        simsync.config._CONFIG_FILE_PATH = self.test_cfg_path

        self.db_path = os.path.join(self.test_dir, "test_backup.db")
        self.db = Database(self.db_path)

        self.config = AppConfig()
        self.config.server.username = "admin"
        self.config.server.password = "MySecurePassword123"
        self.config.server.secret_key = "test-fernet-salt-key-12345"
        self.config.storage.db_path = self.db_path

        self.modem = ModemClient(port="COM_NONE")
        self.notifier = NotificationManager(self.config.notifications)

        self.app = create_app(self.config, self.db, self.modem, notifier=self.notifier)
        self.client = TestClient(self.app)

        # 预先登录获取认证 Cookie
        resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(resp.status_code, 200)
        self.cookies = resp.cookies

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_encrypt_decrypt_value(self):
        secret = "my-secret-key"
        original = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef-123456"
        
        # 加密
        encrypted = encrypt_value(original, secret)
        self.assertTrue(encrypted.startswith("enc:"))
        self.assertNotEqual(encrypted, original)

        # 幂等性：已加密的不重复加密
        re_encrypted = encrypt_value(encrypted, secret)
        self.assertEqual(re_encrypted, encrypted)

        # 解密
        decrypted = decrypt_value(encrypted, secret)
        self.assertEqual(decrypted, original)

        # 空值处理
        self.assertEqual(encrypt_value("", secret), "")
        self.assertEqual(decrypt_value("", secret), "")
        self.assertEqual(decrypt_value("plain-text-no-enc", secret), "plain-text-no-enc")

    def test_encrypt_decrypt_dict_sensitive_fields(self):
        secret = "dict-secret-test"
        sample_dict = {
            "server": {
                "password": "plain-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "api_key": "api-key-123",
            },
            "notifications": {
                "feishu": {
                    "webhook_url": "https://feishu.cn/hook/123",
                    "call_webhook_url": "https://feishu.cn/call/123",
                    "bitable": {
                        "app_secret": "feishu-secret-abc",
                        "app_token": "token-123",
                        "call_app_token": "call-token-456",
                    },
                },
                "email": {
                    "password": "smtp-password-xyz",
                },
                "dingtalk": {
                    "webhook_url": "https://oapi.dingtalk.com/123",
                    "secret": "SEC-dingtalk-abc",
                },
                "telegram": {
                    "bot_token": "123456:bot-token-xyz",
                },
                "webhook": {
                    "url": "https://mywebhook.com/sms",
                    "call_url": "https://mywebhook.com/call",
                }
            }
        }

        # 加密所有敏感字段
        encrypted_dict = encrypt_dict_sensitive_fields(sample_dict, secret)
        self.assertTrue(encrypted_dict["server"]["password"].startswith("enc:"))
        self.assertTrue(encrypted_dict["server"]["totp_secret"].startswith("enc:"))
        self.assertTrue(encrypted_dict["server"]["api_key"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["feishu"]["webhook_url"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["feishu"]["bitable"]["app_secret"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["email"]["password"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["dingtalk"]["secret"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["telegram"]["bot_token"].startswith("enc:"))
        self.assertTrue(encrypted_dict["notifications"]["webhook"]["url"].startswith("enc:"))

        # 解密还原
        decrypted_dict = decrypt_dict_sensitive_fields(encrypted_dict, secret)
        self.assertEqual(decrypted_dict["server"]["password"], "plain-password")
        self.assertEqual(decrypted_dict["notifications"]["feishu"]["webhook_url"], "https://feishu.cn/hook/123")
        self.assertEqual(decrypted_dict["notifications"]["email"]["password"], "smtp-password-xyz")
        self.assertEqual(decrypted_dict["notifications"]["telegram"]["bot_token"], "123456:bot-token-xyz")

    def test_save_and_load_config_with_encryption(self):
        # 1. 开启加密
        self.config.server.encrypt_sensitive_data = True
        self.config.notifications.feishu.webhook_url = "https://feishu.cn/hook/sensitive-token"
        self.config.notifications.email.password = "email-secret-pass"
        ok = save_config(self.config, self.test_cfg_path)
        self.assertTrue(ok)

        # 检查磁盘文件中的内容是否已加密
        with open(self.test_cfg_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
        self.assertIn("enc:", raw_content)
        self.assertNotIn("https://feishu.cn/hook/sensitive-token", raw_content)
        self.assertNotIn("email-secret-pass", raw_content)

        # 2. 从磁盘加载配置，内存中自动解密
        loaded_cfg = load_config(self.test_cfg_path)
        self.assertEqual(loaded_cfg.notifications.feishu.webhook_url, "https://feishu.cn/hook/sensitive-token")
        self.assertEqual(loaded_cfg.notifications.email.password, "email-secret-pass")

        # 3. 关闭加密并写回
        loaded_cfg.server.encrypt_sensitive_data = False
        save_config(loaded_cfg, self.test_cfg_path)
        with open(self.test_cfg_path, "r", encoding="utf-8") as f:
            raw_content_plain = f.read()
        self.assertNotIn("enc:", raw_content_plain)
        self.assertIn("https://feishu.cn/hook/sensitive-token", raw_content_plain)
        self.assertIn("email-secret-pass", raw_content_plain)

    def test_api_backup_export(self):
        # 写入一条短信确保数据库有数据
        self.db.save_sms("+8613800138000", "测试备份短信", direction="inbound")

        # 1. 导出完整 zip
        resp = self.client.get("/api/backup/export?type=full", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn("attachment; filename=simsync_backup_", resp.headers["content-disposition"])

        # 校验 zip 内容
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        self.assertIn("config.yaml", names)
        self.assertIn("simsync.db", names)
        self.assertIn("backup_info.json", names)
        
        info = json.loads(zf.read("backup_info.json"))
        self.assertEqual(info["app"], "SimSync")
        self.assertTrue(info["includes_db"])

        # 2. 仅导出 config.yaml
        resp_cfg = self.client.get("/api/backup/export?type=config", cookies=self.cookies)
        self.assertEqual(resp_cfg.status_code, 200)
        self.assertIn("attachment; filename=config.yaml", resp_cfg.headers["content-disposition"])

        # 3. 仅导出 simsync.db
        resp_db = self.client.get("/api/backup/export?type=db", cookies=self.cookies)
        self.assertEqual(resp_db.status_code, 200)
        self.assertIn("attachment; filename=simsync.db", resp_db.headers["content-disposition"])

    def test_api_backup_restore(self):
        # 准备一个包含新配置的 zip 文件
        new_yaml = """
server:
  username: "restored_admin"
  port: 8099
notifications:
  telegram:
    enabled: true
    bot_token: "restored-telegram-bot-token"
"""
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("config.yaml", new_yaml)
        zip_buf.seek(0)

        files = {
            "file": ("my_backup.zip", zip_buf.getvalue(), "application/zip")
        }
        resp = self.client.post("/api/backup/restore", files=files, cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("成功还原", data["message"])

        # 验证全局 config 已热更新
        self.assertEqual(self.config.server.username, "restored_admin")
        self.assertEqual(self.config.server.port, 8099)
        self.assertTrue(self.config.notifications.telegram.enabled)
        self.assertEqual(self.config.notifications.telegram.bot_token, "restored-telegram-bot-token")

    def test_api_security_encrypt_config(self):
        # 1. 查询当前状态
        resp = self.client.get("/api/security/encrypt_config", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["encrypt_sensitive_data"])

        # 2. 开启加密
        resp_enable = self.client.post("/api/security/encrypt_config", json={"enable": True}, cookies=self.cookies)
        self.assertEqual(resp_enable.status_code, 200)
        self.assertTrue(resp_enable.json()["success"])
        self.assertTrue(resp_enable.json()["encrypt_sensitive_data"])
        self.assertTrue(self.config.server.encrypt_sensitive_data)

        # 3. 关闭加密
        resp_disable = self.client.post("/api/security/encrypt_config", json={"enable": False}, cookies=self.cookies)
        self.assertEqual(resp_disable.status_code, 200)
        self.assertFalse(resp_disable.json()["encrypt_sensitive_data"])
        self.assertFalse(self.config.server.encrypt_sensitive_data)


if __name__ == "__main__":
    unittest.main()

