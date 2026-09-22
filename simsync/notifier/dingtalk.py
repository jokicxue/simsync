import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("simsync.notifier.dingtalk")


class DingTalkNotifier:
    def __init__(self, webhook_url: str, secret: Optional[str] = None):
        self.webhook_url = webhook_url
        self.secret = secret or ""

    def _get_signed_url(self, target_url: str = "", target_secret: str = "") -> str:
        base_url = target_url or self.webhook_url
        secret = target_secret if target_secret else self.secret
        if not secret:
            return base_url

        timestamp = str(round(time.time() * 1000))
        secret_enc = secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}timestamp={timestamp}&sign={sign}"

    def send_sms_notification(self, sms: Dict[str, Any]):
        title = f"📩 新短信: {sms.get('sender')}"
        text = (
            f"### 📩 收到新短信 (SimSync)\n\n"
            f"- **发件人:** {sms.get('sender')}\n"
            f"- **时间:** {sms.get('readable_date')}\n"
            f"- **内容:**\n\n>{sms.get('content')}\n"
        )
        self._send_markdown(title, text)

    def send_call_notification(self, caller: str, action: str, custom_webhook_url: str = "", custom_secret: str = ""):
        title = f"📞 未接来电: {caller}"
        action_desc = "已自动挂断（防漫游扣费）" if (action == "hangup" or action == "auto_hangup") else action
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"### 📞 未接来电提醒 (SimSync)\n\n"
            f"- **来电号码:** {caller}\n"
            f"- **发生时间:** {now_str}\n"
            f"- **处理状态:** {action_desc}\n"
        )
        wh = custom_webhook_url or self.webhook_url
        sec = custom_secret if custom_secret else self.secret
        self._send_markdown(title, text, target_url=wh, target_secret=sec)

    def _send_markdown(self, title: str, text: str, target_url: str = "", target_secret: str = ""):
        post_url = target_url or self.webhook_url
        if not post_url:
            return
        try:
            url = self._get_signed_url(target_url=post_url, target_secret=target_secret)
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
            }
            resp = requests.post(url, json=payload, timeout=8)
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("钉钉机器人消息发送成功")
            else:
                logger.warning(f"钉钉发送失败: {data}")
        except Exception as e:
            logger.error(f"钉钉异常: {e}")
