import logging
import smtplib
import socket
import time
import re
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
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
        self.smtp_host = (smtp_host or "").strip()
        self.smtp_port = int(smtp_port) if smtp_port else 465
        self.use_ssl = bool(use_ssl)
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.from_addr = (from_addr or "").strip()
        self.to_addrs = self._clean_recipients(to_addrs)

    @staticmethod
    def _clean_recipients(addrs: Any) -> List[str]:
        """清洗并解析收件人列表，支持中英文逗号、分号、换行及空白符分隔"""
        if not addrs:
            return []
        if isinstance(addrs, str):
            addrs = [addrs]
        clean_list = []
        for item in addrs:
            tokens = re.split(r"[,，;；\s]+", str(item).strip())
            for t in tokens:
                t = t.strip()
                if t and "@" in t:
                    clean_list.append(t)
        return clean_list

    def _clean_auth_user(self) -> str:
        """获取 SMTP 认证用户名。
        若使用 QQ 邮箱或网易邮箱且仅填入了前缀（如纯数字 QQ 号），自动补全域名后缀，
        防止因服务商协议校验导致连接被直接切断。
        """
        user = self.username.strip()
        if not user:
            return ""
        if "@" not in user:
            host = self.smtp_host.lower()
            if "qq.com" in host and user.isdigit():
                return f"{user}@qq.com"
            elif "163.com" in host:
                return f"{user}@163.com"
            elif "126.com" in host:
                return f"{user}@126.com"
        return user

    def _get_envelope_from(self) -> str:
        """获取 SMTP 协议层发件人 (MAIL FROM)。
        发件人信封地址必须是合法的邮箱格式 (包含 @ 符号)。
        针对 QQ 邮箱 (smtp.qq.com / exmail)、网易邮箱 (163/126) 等国内服务商，
        发件人信封地址通常需与认证用户名 (username) 一致，否则会被服务器返回 501/553 拒绝。
        若未配置发件人或发件人包含 example.com 占位符，自动使用 username。
        """
        clean_from = parseaddr(self.from_addr)[1].strip() if self.from_addr else ""
        clean_user = parseaddr(self.username)[1].strip() or self.username.strip()

        # 针对常见邮件服务商，若用户名未带域名后缀，尝试自动补全
        host_lower = self.smtp_host.lower()
        if clean_user and "@" not in clean_user:
            if "qq.com" in host_lower and clean_user.isdigit():
                clean_user = f"{clean_user}@qq.com"
            elif "163.com" in host_lower:
                clean_user = f"{clean_user}@163.com"
            elif "126.com" in host_lower:
                clean_user = f"{clean_user}@126.com"

        # 若配置了有效的 clean_from
        if clean_from and "@" in clean_from and "example.com" not in clean_from:
            # QQ/网易等严格校验一致性的服务商，若 clean_user 也是有效邮箱，优先使用 clean_user 避免 553
            if any(dom in host_lower for dom in ("qq.com", "163.com", "126.com", "yeah.net", "sina.com")):
                if clean_user and "@" in clean_user:
                    return clean_user
            return clean_from

        # 未配置发件人时，使用 clean_user（必须含 @）
        if clean_user and "@" in clean_user:
            return clean_user

        return clean_from or clean_user

    def send_sms_notification(self, sms: Dict[str, Any]):
        subject = f"[SimSync] 收到来自 {sms.get('sender')} 的新短信"
        plain_text = (
            f"【SimSync 收到新短信】\n"
            f"发件人: {sms.get('sender')}\n"
            f"接收时间: {sms.get('readable_date')}\n"
            f"短信内容:\n{sms.get('content')}\n\n"
            f"此邮件由 SimSync 蜂窝短信网关自动转发并归档。"
        )
        html_content = f"""
        <div style="font-family: Arial, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
            <div style="background: linear-gradient(135deg, #2563eb, #1d4ed8); color: white; padding: 20px 24px;">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600; display: flex; align-items: center; gap: 8px;">📩 收到新短信 (SimSync)</h2>
            </div>
            <div style="padding: 24px; background: #ffffff;">
                <p style="margin: 0 0 8px 0; color: #475569; font-size: 14px;"><strong>发件人：</strong> <span style="color: #0f172a; font-weight: 600;">{sms.get('sender')}</span></p>
                <p style="margin: 0 0 16px 0; color: #475569; font-size: 14px;"><strong>接收时间：</strong> <span style="color: #64748b;">{sms.get('readable_date')}</span></p>
                <div style="background: #f8fafc; border-left: 4px solid #2563eb; border-radius: 4px; padding: 16px; margin: 16px 0; font-size: 15px; line-height: 1.6; color: #1e293b; word-break: break-all;">
                    {sms.get('content')}
                </div>
                <div style="margin-top: 24px; padding-top: 14px; border-top: 1px dashed #e2e8f0; font-size: 12px; color: #94a3b8; text-align: center;">
                    SimSync 蜂窝网关自动推送 &bull; 0 流量漫游防护
                </div>
            </div>
        </div>
        """
        self._send_mail(subject, plain_text, html_content)

    def send_call_notification(self, caller: str, action: str, custom_to_addrs: Optional[List[str]] = None):
        subject = f"[SimSync] 未接来电提醒: {caller}"
        action_desc = "已自动挂断（避免产生国际漫游接听费）" if (action == "hangup" or action == "auto_hangup") else action
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        plain_text = (
            f"【SimSync 未接来电提醒】\n"
            f"来电号码: {caller}\n"
            f"发生时间: {now_str}\n"
            f"处理结果: {action_desc}\n\n"
            f"建议使用网络 VoIP 软件根据来电号码主动回拨。"
        )
        html_content = f"""
        <div style="font-family: Arial, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: auto; border: 1px solid #fee2e2; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
            <div style="background: linear-gradient(135deg, #dc2626, #b91c1c); color: white; padding: 20px 24px;">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600; display: flex; align-items: center; gap: 8px;">📞 未接来电提醒 (SimSync)</h2>
            </div>
            <div style="padding: 24px; background: #ffffff;">
                <p style="margin: 0 0 8px 0; color: #475569; font-size: 14px;"><strong>来电号码：</strong> <span style="color: #dc2626; font-size: 17px; font-weight: 700;">{caller}</span></p>
                <p style="margin: 0 0 8px 0; color: #475569; font-size: 14px;"><strong>发生时间：</strong> <span style="color: #64748b;">{now_str}</span></p>
                <p style="margin: 0 0 16px 0; color: #475569; font-size: 14px;"><strong>处理动作：</strong> <span style="color: #16a34a; font-weight: 600;">{action_desc}</span></p>
                <div style="background: #fef2f2; border-left: 4px solid #dc2626; border-radius: 4px; padding: 12px 16px; margin: 16px 0; font-size: 13px; color: #991b1b; line-height: 1.5;">
                    💡 提示：该来电已被设备自动挂断，以避免在境外漫游或按分钟产生高额通话费。建议根据需要使用网络电话回拨。
                </div>
                <div style="margin-top: 24px; padding-top: 14px; border-top: 1px dashed #e2e8f0; font-size: 12px; color: #94a3b8; text-align: center;">
                    SimSync 蜂窝网关自动推送 &bull; 0 流量漫游防护
                </div>
            </div>
        </div>
        """
        self._send_mail(subject, plain_text, html_content, target_to_addrs=custom_to_addrs)

    def send_reset_code(self, code: str) -> bool:
        """发送密码重置验证码"""
        subject = f"[SimSync] 管理员密码重置验证码: {code}"
        plain_text = f"【SimSync 密码重置验证码】\n本次验证码为: {code} (有效期 10 分钟)。\n若非本人操作请忽略。"
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
            self._send_mail(subject, plain_text, html_content)
            return True
        except Exception:
            return False

    def _send_mail(self, subject: str, plain_text: str, html_body: str, target_to_addrs: Optional[List[str]] = None):
        recipients = self._clean_recipients(target_to_addrs if target_to_addrs is not None else self.to_addrs)
        if not self.smtp_host:
            raise ValueError("未配置 SMTP 服务器地址 (smtp_host)")
        if not self.username or not self.password:
            raise ValueError("未配置 SMTP 登录账号或密码/授权码")
        if not recipients:
            raise ValueError("未配置有效的邮件收件人邮箱 (to_addrs 为空)")

        # 微软个人版邮箱限制拦截 (2024.09.16 起全面封杀 SMTP 基础认证与应用密码)
        user_lower = (self.username or "").lower()
        if any(user_lower.endswith(dom) for dom in ("@outlook.com", "@hotmail.com", "@live.com", "@msn.com")):
            err_msg = (
                "微软个人版 Outlook/Hotmail 邮箱不可用作 SMTP 发信端。\n"
                "【原因】：微软官方已于 2024 年 9 月 16 日彻底废止个人版账户的基本认证 (Basic Auth) 与应用密码 (App Passwords)，强制要求 OAuth 2.0，已无法通过标准 SMTP 协议直接发信。\n"
                "【推荐方案】：请改用 QQ 邮箱 (465端口勾选直接SSL)、网易 163 邮箱或 Gmail 发信；您仍可在“收件人”一栏填写您的 Outlook 邮箱接收推送！"
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg)

        envelope_from = self._get_envelope_from()
        if not envelope_from:
            raise ValueError("发件人地址无效，请填写正确的发件人或登录邮箱账号")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("SimSync", "utf-8")), envelope_from))
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)
        domain = self.smtp_host.split(".", 1)[-1] if "." in self.smtp_host else "simsync.local"
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["MIME-Version"] = "1.0"
        msg["X-Mailer"] = "SimSync-Gateway/1.0"

        # 严格按 RFC alternative 规范：先 text/plain，后 text/html
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        use_ssl = self.use_ssl
        if self.smtp_port == 465:
            use_ssl = True
        elif self.smtp_port in (587, 25):
            use_ssl = False

        server = None
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
                server.ehlo()
                if server.has_extn("starttls"):
                    try:
                        server.starttls()
                        server.ehlo()
                    except Exception as tls_err:
                        logger.warning(f"STARTTLS 升级失败，继续使用现有信道: {tls_err}")

            auth_user = self._clean_auth_user()
            if auth_user and self.password:
                server.login(auth_user, self.password)

            server.sendmail(envelope_from, recipients, msg.as_string())
            logger.info(f"邮件通知发送成功: {recipients} (发件人: {envelope_from})")
            return True
        except smtplib.SMTPAuthenticationError as e:
            raw_err = e.smtp_error.decode("utf-8", errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            err_msg = (
                f"SMTP 身份验证失败 (535)：用户名或授权码不正确。\n"
                f"【重点排查】若使用 QQ 邮箱、163/126 邮箱、Gmail 等，必须在邮箱设置中生成【专用授权码/应用密码】，切勿使用网页日常登录密码！"
                f"（服务商原始响应: {raw_err}）"
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except smtplib.SMTPServerDisconnected as e:
            raw_err = str(e)
            host_lower = self.smtp_host.lower()
            is_qq = "qq.com" in host_lower
            is_163 = "163.com" in host_lower
            is_outlook = any(dom in host_lower for dom in ("outlook.com", "office365.com", "hotmail.com", "live.com"))

            tips = []
            if is_qq:
                tips.append("【QQ 邮箱专用授权码排查】若密码填写了日常登录密码，或授权码过期/错误，QQ 邮箱服务器会直接主动切断连接 (Connection unexpectedly closed) 而非返回 535。\n   👉 请登录 QQ 邮箱网页版 -> 设置 -> 账户 -> 开启 POP3/SMTP 服务，生成专属 16 位英文授权码并填入密码栏。")
                tips.append("【账号格式】QQ 邮箱登录账号请填写完整邮箱地址（如 xxxxxx@qq.com）。")
                tips.append("【端口与 SSL 匹配】QQ 邮箱推荐端口 465 且勾选【使用直接 SSL】；若使用 587 端口请取消勾选直接 SSL。")
            elif is_163:
                tips.append("【网易邮箱客户端授权码】网易 163/126 邮箱必须使用客户端专用授权码，不可使用日常登录密码。")
                tips.append("【端口设置】网易邮箱请使用 465 端口并勾选【使用直接 SSL】（网易不支持 587 端口）。")
            elif is_outlook:
                tips.append("【微软官方政策限制】微软已于 2024 年 9 月 16 日全面废止个人版 Outlook/Hotmail 账户的基本认证 (Basic Auth) 与应用密码 (App Passwords)，强制要求 OAuth 2.0，因此个人版微软邮箱无法作为 SMTP 发信端。\n   👉 强烈建议：改用 QQ 邮箱 (465 SSL)、网易 163 邮箱或 Gmail 发信，将您的 Outlook 邮箱填在“收件人”一栏即可正常接收推送。")
                tips.append("【企业版前提】若为企业版/教育版 Office 365 邮箱，必须由管理员在 Microsoft 365 管理中心为该用户勾选【经过身份验证的 SMTP (Authenticated SMTP)】功能。")
                tips.append("【代理/VPN 拦截 587 端口】若开启了科学上网/Clash/VPN，由于海外代理节点默认封锁 587 端口以防垃圾邮件，会导致连接被代理秒掐断。请在代理软件中将微软服务器设为直连 (DIRECT) 或临时退出代理。")
                tips.append("【端口与 SSL 匹配】Outlook/Office365 推荐 587 端口并【取消勾选使用直接 SSL】（必须走 STARTTLS 协商）。")
            else:
                tips.append("【SSL 端口匹配】通常 465 端口需勾选【使用直接 SSL】；587 或 25 端口需取消勾选（走 STARTTLS）。")
                tips.append("【专用授权码】多数主流邮件服务商强制要求使用【客户端应用密码/授权码】，禁止使用普通登录密码。")
                tips.append("【发件人校验】部分邮件服务器要求发件人信封地址与登录账号必须完全一致。")

            err_msg = (
                f"SMTP 连接被服务器意外关闭 (Connection unexpectedly closed)。\n"
                + "\n".join(f"• {t}" for t in tips)
                + f"\n（当前配置: {self.smtp_host}:{self.smtp_port}，原始信息: {raw_err}）"
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except smtplib.SMTPSenderRefused as e:
            raw_err = e.smtp_error.decode("utf-8", errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            err_msg = (
                f"发件人地址被 SMTP 服务器拒绝 ({e.smtp_code})：发件人必须与登录账号一致。\n"
                f"建议将 Web 设置中的“发件人地址”留空，系统将自动使用登录账号作为发件人。"
                f"（服务商原始响应: {raw_err}）"
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except smtplib.SMTPRecipientsRefused as e:
            err_msg = f"收件人地址被拒绝：请检查收件人邮箱地址是否真实有效。（服务商原始响应: {e.recipients}）"
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except smtplib.SMTPException as e:
            err_msg = f"SMTP 协议异常: {e}"
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except (socket.timeout, TimeoutError) as e:
            err_msg = (
                f"连接 SMTP 服务器超时 (15秒)：请检查服务器地址 ({self.smtp_host}) 与端口 ({self.smtp_port}) 是否正确。"
                f"【提示】465 端口请勾选直接 SSL；587 端口请取消勾选直接 SSL（走 STARTTLS）。并检查宿主机网络出网防火墙。"
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        except Exception as e:
            err_msg = f"邮件发送失败: {e}"
            logger.error(err_msg)
            raise RuntimeError(err_msg) from e
        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
