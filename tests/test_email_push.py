import unittest
from unittest.mock import MagicMock, patch
import smtplib
from simsync.notifier.email_push import EmailNotifier


class TestEmailPush(unittest.TestCase):
    def test_clean_recipients(self):
        # 英文逗号、中文逗号、分号、空格混合
        raw = "user1@example.com, user2@example.com，user3@example.com;user4@qq.com ； user5@163.com"
        cleaned = EmailNotifier._clean_recipients(raw)
        self.assertEqual(len(cleaned), 5)
        self.assertIn("user1@example.com", cleaned)
        self.assertIn("user2@example.com", cleaned)
        self.assertIn("user3@example.com", cleaned)
        self.assertIn("user4@qq.com", cleaned)
        self.assertIn("user5@163.com", cleaned)

    def test_envelope_from(self):
        # QQ 邮箱强制与登录账号一致
        notifier_qq = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="myaccount@qq.com",
            password="auth_password",
            from_addr="simsync@example.com",
            to_addrs=["target@example.com"],
        )
        self.assertEqual(notifier_qq._get_envelope_from(), "myaccount@qq.com")

        # 留空发件人自动 fallback 为 username
        notifier_blank = EmailNotifier(
            smtp_host="smtp.custom.com",
            smtp_port=587,
            use_ssl=False,
            username="admin@custom.com",
            password="pwd",
            from_addr="",
            to_addrs=["target@example.com"],
        )
        self.assertEqual(notifier_blank._get_envelope_from(), "admin@custom.com")

    def test_missing_config_raises(self):
        notifier = EmailNotifier(
            smtp_host="",
            smtp_port=465,
            use_ssl=True,
            username="admin@qq.com",
            password="pwd",
            from_addr="",
            to_addrs=["target@example.com"],
        )
        with self.assertRaises(ValueError):
            notifier.send_sms_notification({"sender": "10086", "content": "test", "readable_date": "2026-09-23"})

        notifier_no_to = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="admin@qq.com",
            password="pwd",
            from_addr="",
            to_addrs=[],
        )
        with self.assertRaises(ValueError):
            notifier_no_to.send_sms_notification({"sender": "10086", "content": "test", "readable_date": "2026-09-23"})

    @patch("smtplib.SMTP_SSL")
    def test_successful_ssl_send(self, mock_smtp_ssl):
        mock_server = MagicMock()
        mock_smtp_ssl.return_value = mock_server

        notifier = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="test@qq.com",
            password="password_or_code",
            from_addr="",
            to_addrs=["target@qq.com"],
        )

        notifier.send_sms_notification({
            "sender": "+8613800138000",
            "content": "验证码 123456",
            "readable_date": "2026-09-23 18:00:00",
        })

        mock_smtp_ssl.assert_called_once_with("smtp.qq.com", 465, timeout=15)
        mock_server.login.assert_called_once_with("test@qq.com", "password_or_code")
        mock_server.sendmail.assert_called_once()
        args = mock_server.sendmail.call_args[0]
        self.assertEqual(args[0], "test@qq.com")
        self.assertEqual(args[1], ["target@qq.com"])

        # 检查邮件消息头是否包含规范字段
        msg_str = args[2]
        self.assertIn("Subject:", msg_str)
        self.assertIn("From:", msg_str)
        self.assertIn("To:", msg_str)
        self.assertIn("Date:", msg_str)
        self.assertIn("Message-ID:", msg_str)
        self.assertIn("MIME-Version:", msg_str)
        # 包含 plain text 与 html
        self.assertIn("text/plain", msg_str)
        self.assertIn("text/html", msg_str)

    @patch("smtplib.SMTP_SSL")
    def test_auth_error_translation(self, mock_smtp_ssl):
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")
        mock_smtp_ssl.return_value = mock_server

        notifier = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="test@qq.com",
            password="wrong_password",
            from_addr="",
            to_addrs=["target@qq.com"],
        )

        with self.assertRaises(RuntimeError) as ctx:
            notifier.send_sms_notification({
                "sender": "10086",
                "content": "test",
                "readable_date": "2026-09-23",
            })
        self.assertIn("SMTP 身份验证失败 (535)", str(ctx.exception))
        self.assertIn("专用授权码", str(ctx.exception))

    def test_clean_auth_user(self):
        # QQ 纯数字自动补全 @qq.com
        notifier_qq = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="12345678",
            password="pwd",
            from_addr="",
            to_addrs=["target@qq.com"],
        )
        self.assertEqual(notifier_qq._clean_auth_user(), "12345678@qq.com")
        self.assertEqual(notifier_qq._get_envelope_from(), "12345678@qq.com")

        # 完整邮箱保持原样
        notifier_full = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="test@qq.com",
            password="pwd",
            from_addr="",
            to_addrs=["target@qq.com"],
        )
        self.assertEqual(notifier_full._clean_auth_user(), "test@qq.com")

    @patch("smtplib.SMTP_SSL")
    def test_server_disconnected_diagnosis(self, mock_smtp_ssl):
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        mock_smtp_ssl.return_value = mock_server

        notifier = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            use_ssl=True,
            username="12345678@qq.com",
            password="wrong_password",
            from_addr="",
            to_addrs=["target@qq.com"],
        )

        with self.assertRaises(RuntimeError) as ctx:
            notifier.send_sms_notification({
                "sender": "10086",
                "content": "test",
                "readable_date": "2026-09-23",
            })
        err_text = str(ctx.exception)
        self.assertIn("Connection unexpectedly closed", err_text)
        self.assertIn("QQ 邮箱专用授权码排查", err_text)
        self.assertIn("POP3/SMTP", err_text)


if __name__ == "__main__":
    unittest.main()
