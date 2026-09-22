import logging
import time
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("simsync.notifier.telegram")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, proxy: Optional[str] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy = proxy

    def send_sms_notification(self, sms: Dict[str, Any]):
        text = (
            f"📩 <b>收到新短信 (SimSync)</b>\n\n"
            f"<b>发件人:</b> <code>{sms.get('sender')}</code>\n"
            f"<b>时间:</b> {sms.get('readable_date')}\n"
            f"<b>内容:</b>\n{sms.get('content')}"
        )
        self._send_message(text)

    def send_call_notification(self, caller: str, action: str, custom_chat_id: str = ""):
        action_desc = "已自动挂断（防漫游扣费）" if (action == "hangup" or action == "auto_hangup") else action
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"📞 <b>未接来电提醒 (SimSync)</b>\n\n"
            f"<b>来电号码:</b> <code>{caller}</code>\n"
            f"<b>发生时间:</b> {now_str}\n"
            f"<b>处理状态:</b> {action_desc}"
        )
        cid = custom_chat_id or self.chat_id
        self._send_message(text, target_chat_id=cid)

    def _send_message(self, html_text: str, target_chat_id: str = ""):
        cid = target_chat_id or self.chat_id
        if not self.bot_token or not cid:
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": cid,
                "text": html_text,
                "parse_mode": "HTML",
            }
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            resp = requests.post(url, json=payload, timeout=10, proxies=proxies)
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram 消息发送成功")
            else:
                logger.warning(f"Telegram 发送失败: {data}")
        except Exception as e:
            logger.error(f"Telegram 发送异常: {e}")
