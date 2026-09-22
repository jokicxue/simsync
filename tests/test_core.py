import os
import shutil
import tempfile
import unittest
from simsync.storage.database import Database
from simsync.storage.xml_exporter import generate_sms_backup_xml
from simsync.modem.pdu_decoder import PduDecoder, parse_text_mode_sms
from simsync.modem.call_forward import CallForwardController


class TestSimSyncCore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_simsync.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_database_sms_and_calls(self):
        # 测试短信存储
        sms_id = self.db.save_sms(
            sender="+61412345678",
            content="Your Aldi Mobile verification code is 889900.",
            timestamp_ms=1726800000000,
            readable_date="2026-09-20 12:00:00",
            smsc="+61411990001",
        )
        self.assertGreater(sms_id, 0)

        sms_list = self.db.get_all_sms()
        self.assertEqual(len(sms_list), 1)
        self.assertEqual(sms_list[0]["sender"], "+61412345678")
        self.assertIn("889900", sms_list[0]["content"])

        # 测试来电记录
        call_id = self.db.save_call(
            caller="+61498765432",
            action="hangup",
        )
        self.assertGreater(call_id, 0)

        call_list = self.db.get_all_calls()
        self.assertEqual(len(call_list), 1)
        self.assertEqual(call_list[0]["caller"], "+61498765432")
        self.assertEqual(call_list[0]["action"], "hangup")

    def test_xml_export_format(self):
        # 插入两条测试短信
        self.db.save_sms(
            sender="+61411112222",
            content="Test SMS 1",
            timestamp_ms=1726800001000,
            readable_date="2026-09-20 12:00:01",
        )
        self.db.save_sms(
            sender="AldiMobile",
            content="Welcome to Aldi Mobile Australia!",
            timestamp_ms=1726800002000,
            readable_date="2026-09-20 12:00:02",
        )

        all_sms = self.db.get_sms_for_export()
        xml_str = generate_sms_backup_xml(all_sms)

        self.assertTrue(xml_str.startswith("<?xml version='1.0' encoding='UTF-8'"))
        self.assertIn("<smses count=\"2\"", xml_str)
        self.assertIn("address=\"+61411112222\"", xml_str)
        self.assertIn("body=\"Welcome to Aldi Mobile Australia!\"", xml_str)
        self.assertIn("type=\"1\"", xml_str)
        self.assertIn("read=\"1\"", xml_str)

    def test_text_mode_sms_fallback(self):
        header = '+CMT: "+61412345678","","26/09/20,12:30:00+40"'
        body = "Hello from Telstra network"
        parsed = parse_text_mode_sms(header, body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["sender"], "+61412345678")
        self.assertEqual(parsed["content"], "Hello from Telstra network")

    def test_ucs2_pdu_decoding(self):
        decoder = PduDecoder()
        # 构造一条真实的 UCS2 SMS-DELIVER PDU:
        # SMSC: 00 (无内置SMSC)
        # First Octet: 04 (SMS-DELIVER, 无 UDH)
        # Sender: 0B 91 1614325476F8 -> +61412345678
        # TP-PID: 00
        # TP-DCS: 08 (UCS2 编码)
        # TP-SCTS: 62900221000000 (2026-09-20 12:00:00)
        # TP-UDL: 08 (8 字节 / 4 个 UCS2 字符)
        # TP-UD: 4F60597D (你好)
        pdu = "00040B911614325476F8000862900221000000044F60597D"
        res = decoder.decode(pdu)
        self.assertIsNotNone(res)
        self.assertEqual(res["sender"], "+61412345678")
        self.assertEqual(res["content"], "你好")
        self.assertTrue(res["is_complete"])

    def test_call_forward_parsing(self):
        def mock_send_at(cmd, timeout=3.0):
            if "AT+CCFC=0,2" in cmd:
                # 模拟已激活无条件转移到 +61488888888
                return '+CCFC: 1,1,"+61488888888",145\nOK'
            elif "AT+CCFC=0,0" in cmd:
                return "OK"
            return "ERROR"

        cf = CallForwardController(mock_send_at)
        res = cf.query(0)
        self.assertTrue(res["success"])
        self.assertTrue(res["active"])
        self.assertEqual(res["number"], "+61488888888")

        deact_res = cf.deactivate(0)
        self.assertTrue(deact_res["success"])

    def test_pdu_encoding(self):
        from simsync.modem.pdu_decoder import encode_sms_submit_pdu

        # 1. 短短信编码测试
        pdus = encode_sms_submit_pdu("+61412345678", "Hello 你好")
        self.assertEqual(len(pdus), 1)
        cmgs_len, pdu_hex = pdus[0]
        self.assertGreater(cmgs_len, 10)
        self.assertTrue(pdu_hex.startswith("000100"))  # SMSC=00, MTI=01, MR=00
        self.assertIn("1614325476F8", pdu_hex)  # 交换后的号码
        self.assertIn("00480065006C006C006F00204F60597D", pdu_hex)  # "Hello 你好" 的 UCS2 编码

        # 2. 长短信 (>70 字符) 自动分片测试
        long_text = "这是一条非常长非常长的测试短信，用于验证合宙 Air780E 在 PDU 模式下的 UDH 自动分片功能。" * 3
        long_pdus = encode_sms_submit_pdu("+61412345678", long_text)
        self.assertGreater(len(long_pdus), 1)
        for cmgs_len, pdu_hex in long_pdus:
            # 第一字节应该是含 UDHI 标志的 41
            self.assertTrue(pdu_hex.startswith("004100"))

    def test_conversations_and_outbound_sms(self):
        # 存入一条接收短信
        self.db.save_sms(
            sender="+61400112233",
            content="收到来自澳洲的短信",
            timestamp_ms=1726800000000,
            readable_date="2026-09-20 12:00:00",
            direction="inbound",
        )
        # 存入一条回复短信
        self.db.save_outbound_sms(
            recipient="+61400112233",
            content="这是通过 Web 控制台发出的回复",
            timestamp_ms=1726800060000,
        )

        # 验证会话列表
        convs = self.db.get_conversations()
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["phone"], "+61400112233")
        self.assertEqual(convs[0]["count"], 2)
        self.assertEqual(convs[0]["last_message"], "这是通过 Web 控制台发出的回复")

        # 验证会话内往来消息
        msgs = self.db.get_conversation_messages("+61400112233")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["direction"], "inbound")
        self.assertEqual(msgs[1]["direction"], "outbound")

    def test_scheduled_tasks(self):
        # 创建保号任务
        task_id = self.db.add_scheduled_task(
            name="Aldi 保号",
            recipient="+61400000000",
            content="Balance",
            interval_days=90,
        )
        self.assertGreater(task_id, 0)

        tasks = self.db.get_scheduled_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Aldi 保号")
        self.assertEqual(tasks[0]["enabled"], 1)

        # 更新任务
        self.db.update_scheduled_task(task_id, next_run_at="2020-01-01 00:00:00")
        due_tasks = self.db.get_due_tasks()
        self.assertEqual(len(due_tasks), 1)
        self.assertEqual(due_tasks[0]["id"], task_id)

        # 删除任务
        self.db.delete_scheduled_task(task_id)
        self.assertEqual(len(self.db.get_scheduled_tasks()), 0)


if __name__ == "__main__":
    unittest.main()

