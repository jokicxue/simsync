import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from simsync.config import AppConfig, load_config, save_config
from simsync.storage.database import Database
from simsync.storage.crypto import DataCipher
from simsync.notifier.feishu import FeishuNotifier
from simsync.notifier.manager import NotificationManager
from simsync.web.app import create_app
from fastapi.testclient import TestClient


class TestSyncAndConfigBackup(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.test_dir, "config.yaml")
        self.db_path = os.path.join(self.test_dir, "simsync.db")

        self.cipher = DataCipher(password="test_secret_123")
        self.db = Database(self.db_path, cipher=self.cipher)

        self.config = AppConfig()
        self.config.storage.db_path = self.db_path
        self.config.server.username = "admin_user"
        self.config.server.password_hash = "d033e22ae348aeb5660fc2140aec35850c4da997"
        self.config.modem.phone_number = "+8613800138000"
        self.config.notifications.feishu.enabled = True
        self.config.notifications.feishu.bitable.enabled = True
        self.config.notifications.feishu.bitable.app_id = "cli_test_123"
        self.config.notifications.feishu.bitable.app_secret = "sec_test_456"
        self.config.notifications.feishu.bitable.app_token = "bascnTestAppToken"
        self.config.notifications.feishu.bitable.table_id = "tblTestTable"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ==================== 1. 配置双重持久化与自愈恢复测试 ====================

    def test_config_dual_persistence_and_auto_restore(self):
        """测试配置双重持久化至 SQLite，以及在 YAML 被初始空模板覆盖后自动从 SQLite 恢复"""
        # 1. 保存配置，预期 YAML 和 SQLite system_meta 均有记录
        ok = save_config(self.config, self.cfg_path)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(self.cfg_path))

        # 验证 SQLite system_meta 中已记录 backup_config_json
        backup_dict = self.db.load_config_backup()
        self.assertIsNotNone(backup_dict)
        self.assertEqual(backup_dict["server"]["username"], "admin_user")
        self.assertEqual(backup_dict["modem"]["phone_number"], "+8613800138000")
        self.assertTrue(backup_dict["notifications"]["feishu"]["enabled"])

        # 2. 模拟代码更新操作：用户下载了新代码包，自带的初始空 config.yaml 覆盖了原配置
        empty_config = AppConfig()
        empty_config.storage.db_path = self.db_path  # db_path 依然指向 db
        # 写入空 YAML (无密码、无推送)
        import yaml
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(empty_config.model_dump(), f)

        # 验证此时 YAML 文件中为无密码和无推送
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
        self.assertEqual(content["server"]["password"], "")
        self.assertEqual(content["server"]["password_hash"], "")
        self.assertFalse(content["notifications"]["feishu"]["enabled"])

        # 3. 启动调用 load_config，验证自动识别出空模板并从 SQLite 恢复
        restored_cfg = load_config(self.cfg_path)
        self.assertEqual(restored_cfg.server.username, "admin_user")
        self.assertEqual(restored_cfg.server.password_hash, "d033e22ae348aeb5660fc2140aec35850c4da997")
        self.assertEqual(restored_cfg.modem.phone_number, "+8613800138000")
        self.assertTrue(restored_cfg.notifications.feishu.enabled)

        # 验证 YAML 文件已被自动回写恢复
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            recovered_yaml = yaml.safe_load(f)
        self.assertEqual(recovered_yaml["server"]["password_hash"], "d033e22ae348aeb5660fc2140aec35850c4da997")
        self.assertTrue(recovered_yaml["notifications"]["feishu"]["enabled"])

    # ==================== 2. 数据库 receiver 字段测试 ====================

    def test_database_receiver_field_and_query(self):
        """测试短信记录的 receiver 字段存储、加密与按号码筛选"""
        # 保存 3 条短信：2 条属于 +8613800138000，1 条属于 +8613900139000
        self.db.save_sms(
            sender="10086",
            content="验证码 1111",
            readable_date="2026-09-21 10:00:00",
            receiver="+8613800138000",
        )
        self.db.save_sms(
            sender="10010",
            content="验证码 2222",
            readable_date="2026-09-21 11:00:00",
            receiver="+8613800138000",
        )
        self.db.save_sms(
            sender="10000",
            content="电信通知 3333",
            readable_date="2026-09-21 12:00:00",
            receiver="+8613900139000",
        )

        all_sms = self.db.get_all_sms()
        self.assertEqual(len(all_sms), 3)
        self.assertEqual(all_sms[0]["receiver"], "+8613900139000")

        # 按 receiver 筛选
        card1_sms = self.db.get_sms_by_receiver("+8613800138000")
        self.assertEqual(len(card1_sms), 2)
        self.assertEqual(card1_sms[0]["content"], "验证码 1111")
        self.assertEqual(card1_sms[1]["content"], "验证码 2222")

        card2_sms = self.db.get_sms_by_receiver("+8613900139000")
        self.assertEqual(len(card2_sms), 1)
        self.assertEqual(card2_sms[0]["content"], "电信通知 3333")

    # ==================== 3. 飞书多维表格按手机号比对与增量同步测试 ====================

    @patch("requests.post")
    @patch("requests.get")
    def test_feishu_bitable_smart_sync(self, mock_get, mock_post):
        """测试飞书多维表格智能比对：隔离手机号、指纹去重、批量补录缺失短信"""
        notifier = FeishuNotifier(
            webhook_url="",
            bitable_config=self.config.notifications.feishu.bitable.model_dump(),
        )
        # Mock 获取 tenant token
        notifier._get_tenant_access_token = MagicMock(return_value="t-fake-token-123")

        # 本地数据库插入 3 条短信（属于当前卡号 +8613800138000）
        self.db.save_sms(sender="10086", content="短信A", readable_date="2026-09-21 09:00:00", receiver="+8613800138000")
        self.db.save_sms(sender="10086", content="短信B", readable_date="2026-09-21 10:00:00", receiver="+8613800138000")
        self.db.save_sms(sender="10010", content="短信C", readable_date="2026-09-21 11:00:00", receiver="+8613800138000")

        # 模拟飞书多维表格已存在 2 条记录：
        # 1条属于 +8613800138000 的「短信A」（已存在，应跳过）
        # 1条属于 另一个号码 +8613999999999 的「短信X」（不属于本卡，不参与指纹匹配）
        mock_get.return_value.json.return_value = {
            "code": 0,
            "data": {
                "has_more": False,
                "items": [
                    {
                        "fields": {
                            "本机号码": "+8613800138000",
                            "发件人": "10086",
                            "短信内容": "短信A",
                            "接收时间": "2026-09-21 09:00:00",
                        }
                    },
                    {
                        "fields": {
                            "本机号码": "+8613999999999",
                            "发件人": "95588",
                            "短信内容": "短信X (他卡)",
                            "接收时间": "2026-09-21 08:00:00",
                        }
                    }
                ]
            }
        }

        # 模拟批量创建成功
        mock_post.return_value.json.return_value = {"code": 0}

        # 执行同步比对
        result = notifier.sync_sms_from_db(self.db, phone_number="+8613800138000")

        self.assertTrue(result["success"])
        self.assertEqual(result["total_local"], 3)
        self.assertEqual(result["feishu_existing"], 1)  # 仅匹配本卡 1 条
        self.assertEqual(result["synced_new"], 2)       # 缺失「短信B」和「短信C」，共补录 2 条

        # 验证批量创建接口被调用
        mock_post.assert_called_once()
        call_payload = mock_post.call_args[1]["json"]
        records_to_insert = call_payload["records"]
        self.assertEqual(len(records_to_insert), 2)
        self.assertEqual(records_to_insert[0]["fields"]["短信内容"], "短信B")
        self.assertEqual(records_to_insert[1]["fields"]["短信内容"], "短信C")
        self.assertEqual(records_to_insert[0]["fields"]["本机号码"], "+8613800138000")

    # ==================== 4. Web API 端点测试 ====================

    @patch("simsync.notifier.feishu.FeishuNotifier.sync_sms_from_db")
    def test_sync_bitable_endpoint(self, mock_sync):
        """测试 Web 控制台 /api/notifications/feishu/bitable/sync 端点权限与调用"""
        mock_sync.return_value = {
            "success": True,
            "total_local": 10,
            "feishu_existing": 8,
            "synced_new": 2,
            "message": "同步完成",
        }

        modem_mock = MagicMock()
        modem_mock.phone_number = "+8613800138000"

        notifier = NotificationManager(self.config.notifications)
        app = create_app(self.config, self.db, modem_mock, notifier=notifier)
        client = TestClient(app)

        # 1. 未登录访问应返回 401
        res = client.post("/api/notifications/feishu/bitable/sync")
        self.assertEqual(res.status_code, 401)

        # 2. 登录后访问
        client.post(
            "/api/login",
            json={"username": "admin_user", "password": "wrong"},
        )
        # 正常登录
        from simsync.web.app import hmac, hashlib
        login_res = client.post(
            "/api/login",
            json={"username": "admin_user", "password": ""},
        )
        # 绕过或直接以有效 token 测试
        import time
        token_key = f"{self.config.server.secret_key or 'simsync-secret-key'}:{self.config.server.password_hash}"
        token_str = str(int(time.time()))
        sig = hmac.new(
            token_key.encode("utf-8"),
            token_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        client.cookies.set("simsync_token", f"{token_str}.{sig}")

        sync_resp = client.post("/api/notifications/feishu/bitable/sync")
        self.assertEqual(sync_resp.status_code, 200)
        data = sync_resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["synced_new"], 2)


if __name__ == "__main__":
    unittest.main()
