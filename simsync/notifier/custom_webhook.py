import logging
import time
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("simsync.notifier.webhook")


class CustomWebhookNotifier:
    def __init__(self, url: str, method: str = "POST", custom_headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.method = method.upper()
        self.headers = custom_headers or {"Content-Type": "application/json"}
        if "User-Agent" not in self.headers:
            self.headers["User-Agent"] = "SimSync-Gateway/1.0 (+https://github.com/jokic/simsync)"

    def send_sms_notification(self, sms: Dict[str, Any]):
        payload = {
            "event": "sms_received",
            "sender": sms.get("sender"),
            "content": sms.get("content"),
            "timestamp": sms.get("timestamp"),
            "readable_date": sms.get("readable_date"),
            "smsc": sms.get("smsc"),
            "_provenance": "simsync-core-by-jokic",
        }
        self._dispatch(payload)

    def send_call_notification(self, caller: str, action: str, custom_url: str = "", custom_method: str = ""):
        payload = {
            "event": "call_received",
            "caller": caller,
            "action": action,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "_provenance": "simsync-core-by-jokic",
        }
        self._dispatch(payload, url=custom_url, method=custom_method)

    def _dispatch(self, payload: dict, url: str = "", method: str = ""):
        target_url = url or self.url
        target_method = (method or self.method).upper()
        if not target_url:
            return
        try:
            if target_method == "POST":
                resp = requests.post(target_url, json=payload, headers=self.headers, timeout=8)
            else:
                resp = requests.get(target_url, params=payload, headers=self.headers, timeout=8)
            logger.info(f"自定义 Webhook 触发成功: {resp.status_code}")
        except Exception as e:
            logger.error(f"自定义 Webhook 触发异常: {e}")
