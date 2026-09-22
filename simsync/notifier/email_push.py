import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List, Optional

logger = logging.getLogger("simsync.notifier.email")


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        use_ssl: bool,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: List[str],
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.use_ssl = use_ssl
        self.username = username
        self.password = password
        self.from_addr = from_addr or username
        self.to_addrs = to_addrs

    def send_sms_notification(self, sms: Dict[str, Any]):
        subject = f"[SimSync] 收到来自 {sms.get('sender')} 的新短信"
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
            <h2 style="color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 10px;">📩 收到新短信</h2>
            <p><strong>发件人：</strong> <span style="color: #202124;">{sms.get('sender')}</span></p>
            <p><strong>时间：</strong> <span style="color: #5f6368;">{sms.get('readable_date')}</span></p>
            <div style="background: #f8f9fa; border-left: 4px solid #1a73e8; padding: 15px; margin: 15px 0; font-size: 16px; line-height: 1.5; color: #3c4043;">
                {sms.get('content')}
            </div>
            <p style="font-size: 12px; color: #80868b; margin-top: 20px;">此邮件由 SimSync 系统自动发送并永久归档备份。</p>
        </div>
        """
        self._send_mail(subject, html_content)

    def send_call_notification(self, caller: str, action: str, custom_to_addrs: Optional[List[str]] = None):
        subject = f"[SimSync] 未接来电提醒: {caller}"
        action_desc = "已自动挂断（避免产生国际漫游接听费）" if (action == "hangup" or action == "auto_hangup") else action
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
            <h2 style="color: #d93025; border-bottom: 2px solid #d93025; padding-bottom: 10px;">📞 未接来电提醒</h2>
            <p><strong>来电号码：</strong> <span style="color: #d93025; font-size: 16px; font-weight: bold;">{caller}</span></p>
            <p><strong>发生时间：</strong> <span style="color: #5f6368;">{now_str}</span></p>
            <p><strong>处理结果：</strong> <span style="color: #137333;">{action_desc}</span></p>
            <p style="font-size: 12px; color: #80868b; margin-top: 20px;">建议使用 VoIP 软件（如 Skype / 微信）根据号码主动回拨。</p>
        </div>
        """
        self._send_mail(subject, html_content, target_to_addrs=custom_to_addrs)

    def send_reset_code(self, code: str) -> bool:
        """发送密码重置验证码"""
        subject = f"[SimSync] 管理员密码重置验证码: {code}"
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px;">
            <h2 style="color: #2563eb; margin-bottom: 12px;">🔒 密码重置验证码</h2>
            <p style="color: #475569; font-size: 14px;">您正在通过 SimSync 控制台请求重置密码，本次验证码为：</p>
            <div style="background: #f1f5f9; padding: 16px; border-radius: 8px; text-align: center; margin: 18px 0;">
                <span style="font-size: 28px; font-weight: 700; letter-spacing: 6px; color: #0f172a;">{code}</span>
            </div>
            <p style="color: #64748b; font-size: 12px; line-height: 1.5;">
                * 验证码有效期为 10 分钟。若非本人操作，请忽略此邮件并检查公网访问安全性。
            </p>
        </div>
        """
        try:
            self._send_mail(subject, html_content)
            return True
        except Exception:
            return False

    def _send_mail(self, subject: str, html_body: str, target_to_addrs: Optional[List[str]] = None):
        recipients = target_to_addrs if target_to_addrs is not None else self.to_addrs
        if not self.smtp_host or not recipients:
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(recipients)

        part = MIMEText(html_body, "html", "utf-8")
        msg.attach(part)

        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10)
                try:
                    server.starttls()
                except Exception:
                    pass

            if self.username and self.password:
                server.login(self.username, self.password)

            server.sendmail(self.from_addr, recipients, msg.as_string())
            server.quit()
            logger.info(f"邮件通知发送成功: {recipients}")
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
