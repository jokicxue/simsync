import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import simsync.config
from simsync.config import AppConfig, NotificationConfig
from simsync.storage.database import Database
from simsync.storage.xml_exporter import generate_call_logs_backup_xml
from simsync.notifier.manager import NotificationManager
from simsync.modem.at_client import ModemClient
from simsync.web.app import create_app


class TestCallAndChannels(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_cfg_path = os.path.join(self.test_dir, "test_config.yaml")
        simsync.config._CONFIG_FILE_PATH = self.test_cfg_path

        self.db_path = os.path.join(self.test_dir, "test_channels.db")
        self.db = Database(self.db_path)

        self.config = AppConfig()
        self.config.server.username = "admin"
        self.config.server.password = "MySecurePassword123"
        self.config.storage.db_path = self.db_path

        self.modem = ModemClient(port="COM_NONE")
        self.notifier = NotificationManager(self.config.notifications)

        self.app = create_app(self.config, self.db, self.modem, notifier=self.notifier)
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_generate_call_logs_backup_xml(self):
        calls = [
            {
                "id": 1,
                "caller": "+61412345678",
                "action": "auto_hangup",
                "created_at": "2026-09-20 12:00:00",
            },
            {
                "id": 2,
                "caller": "10086",
                "action": "missed",
                "created_at": "2026-09-20 12:05:00",
            }
        ]
        xml_content = generate_call_logs_backup_xml(calls)
        self.assertTrue(xml_content.startswith("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"))
        self.assertIn('<calls count="2"', xml_content)
        self.assertIn('number="+61412345678"', xml_content)
        self.assertIn('number="10086"', xml_content)
        self.assertIn('type="5"', xml_content)  # 5 = rejected / auto_hangup
        self.assertIn('type="3"', xml_content)  # 3 = missed call

    def test_export_calls_xml_endpoint(self):
        # 登录
        login_resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(login_resp.status_code, 200)
        cookies = login_resp.cookies

        # 插入通话记录
        self.db.save_call(caller="+61411223344", action="auto_hangup")

        # 导出 XML
        resp = self.client.get("/api/export/calls_xml", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/xml", resp.headers.get("content-type", ""))
        self.assertIn("calls_backup.xml", resp.headers.get("content-disposition", ""))
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        self.assertIn("+61411223344", resp.text)

    def test_notifications_config_update_with_call_settings(self):
        login_resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        cookies = login_resp.cookies

        payload = {
            "feishu": {
                "enabled": True,
                "webhook_url": "https://open.feishu.cn/hook/sms",
                "call_enabled": True,
                "call_use_custom": True,
                "call_webhook_url": "https://open.feishu.cn/hook/call",
                "bitable": {
                    "enabled": True,
                    "app_id": "cli_test",
                    "app_secret": "sec_test",
                    "app_token": "app_token_test",
                    "table_id": "tbl_sms",
                    "call_enabled": True,
                    "call_mode": "same_base",
                    "call_table_id": "tbl_calls",
                    "call_app_token": "",
                }
            },
            "wechat": {
                "enabled": True,
                "mode": "wecom_bot",
                "wecom_bot": {"webhook_url": "https://qyapi.weixin.qq.com/sms"},
                "wxpusher": {"app_token": "AT_test", "uids": ["UID_1"]},
                "call_enabled": True,
                "call_use_custom": True,
                "call_wecom_webhook_url": "https://qyapi.weixin.qq.com/call",
                "call_wxpusher_uids": ["UID_CALL"]
            },
            "email": {
                "enabled": True,
                "smtp_host": "smtp.test.com",
                "smtp_port": 465,
                "use_ssl": True,
                "username": "u@test.com",
                "password": "pwd",
                "from_addr": "u@test.com",
                "to_addrs": ["sms@test.com"],
                "call_enabled": True,
                "call_use_custom": True,
                "call_to_addrs": ["call@test.com"]
            },
            "dingtalk": {
                "enabled": True,
                "webhook_url": "https://dingtalk/sms",
                "secret": "SEC_sms",
                "call_enabled": True,
                "call_use_custom": True,
                "call_webhook_url": "https://dingtalk/call",
                "call_secret": "SEC_call"
            },
            "telegram": {
                "enabled": True,
                "bot_token": "bot:token",
                "chat_id": "111",
                "proxy": "",
                "call_enabled": True,
                "call_use_custom": True,
                "call_chat_id": "222"
            },
            "webhook": {
                "enabled": True,
                "url": "https://webhook/sms",
                "method": "POST",
                "call_enabled": True,
                "call_use_custom": True,
                "call_url": "https://webhook/call",
                "call_method": "GET"
            }
        }

        resp = self.client.post("/api/notifications/config", json=payload, cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

        # 检查内存中的配置已更新
        cfg = self.config.notifications
        self.assertTrue(cfg.feishu.call_enabled)
        self.assertTrue(cfg.feishu.call_use_custom)
        self.assertEqual(cfg.feishu.call_webhook_url, "https://open.feishu.cn/hook/call")
        self.assertTrue(cfg.feishu.bitable.call_enabled)
        self.assertEqual(cfg.feishu.bitable.call_mode, "same_base")
        self.assertEqual(cfg.feishu.bitable.call_table_id, "tbl_calls")

        self.assertTrue(cfg.wechat.call_enabled)
        self.assertEqual(cfg.wechat.call_wecom_webhook_url, "https://qyapi.weixin.qq.com/call")
        self.assertEqual(cfg.email.call_to_addrs, ["call@test.com"])
        self.assertEqual(cfg.dingtalk.call_webhook_url, "https://dingtalk/call")
        self.assertEqual(cfg.telegram.call_chat_id, "222")
        self.assertEqual(cfg.webhook.call_url, "https://webhook/call")
        self.assertEqual(cfg.webhook.call_method, "GET")

    def test_call_dispatch_and_testing(self):
        # 初始化所有通道并测试 NotificationManager 的 dispatch_call 和 test_channel
        notif = self.notifier
        notif.feishu = MagicMock()
        notif.wechat = MagicMock()
        notif.email = MagicMock()
        notif.dingtalk = MagicMock()
        notif.telegram = MagicMock()
        notif.webhook = MagicMock()

        notif.dispatch_call(caller="+61412345678", action="auto_hangup")
        time.sleep(0.3)

        notif.feishu.send_call_notification.assert_called_once()
        notif.wechat.send_call_notification.assert_called_once()
        notif.email.send_call_notification.assert_called_once()
        notif.dingtalk.send_call_notification.assert_called_once()
        notif.telegram.send_call_notification.assert_called_once()
        notif.webhook.send_call_notification.assert_called_once()

        # 测试 test_channel with event_type="call"
        res = notif.test_channel("feishu", event_type="call")
        self.assertTrue(res["success"])
        self.assertEqual(notif.feishu.send_call_notification.call_count, 2)

    def test_phone_number_cnum_and_endpoint(self):
        # 1. 模拟 AT+CNUM 自动读取
        def mock_send_at(cmd, timeout=3.0):
            if cmd == "AT+CNUM":
                return '+CNUM: "","+8613800138000",145\nOK'
            elif cmd == "AT+CFUN?":
                return '+CFUN: 1\nOK'
            return "OK"

        self.modem.send_at = mock_send_at
        self.modem.phone_number = ""
        self.modem.refresh_status()
        self.assertEqual(self.modem.phone_number, "+8613800138000")

        # 2. 登录并测试 /api/modem/phone_number
        login_resp = self.client.post("/api/login", json={"username": "admin", "password": "MySecurePassword123"})
        self.assertEqual(login_resp.status_code, 200)
        cookies = login_resp.cookies

        set_resp = self.client.post(
            "/api/modem/phone_number",
            json={"phone_number": "+8613999999999"},
            cookies=cookies,
        )
        self.assertEqual(set_resp.status_code, 200)
        self.assertTrue(set_resp.json()["success"])
        self.assertEqual(self.modem.phone_number, "+8613999999999")
        self.assertEqual(self.config.modem.phone_number, "+8613999999999")

        # 3. 检查 /api/status 返回字段
        status_resp = self.client.get("/api/status", cookies=cookies)
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()["phone_number"], "+8613999999999")

        # 4. 检查首页渲染包含设置的号码
        index_resp = self.client.get("/", cookies=cookies)
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn("+8613999999999", index_resp.text)


if __name__ == "__main__":
    unittest.main()
