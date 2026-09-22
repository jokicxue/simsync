import logging
import time
from typing import Dict, Any, List
import requests

logger = logging.getLogger("simsync.notifier.wechat")


class WechatNotifier:
    def __init__(self, mode: str = "wecom_bot", wecom_webhook: str = "", wxpusher_token: str = "", wxpusher_uids: List[str] = None):
        self.mode = mode
        self.wecom_webhook = wecom_webhook
        self.wxpusher_token = wxpusher_token
        self.wxpusher_uids = wxpusher_uids or []

    def send_sms_notification(self, sms: Dict[str, Any]):
        title = f"📩 新短信: {sms.get('sender')}"
        content_md = (
            f"### 📩 收到新短信 (SimSync)\n"
            f"> **发件人:** <font color=\"info\">{sms.get('sender')}</font>\n"
            f"> **时间:** {sms.get('readable_date')}\n"
            f"> **内容:** {sms.get('content')}\n"
        )
        self._dispatch(title, content_md, sms.get("content", ""))

    def send_call_notification(self, caller: str, action: str, custom_webhook: str = "", custom_uids: List[str] = None):
        title = f"📞 未接来电: {caller}"
        action_desc = "已自动挂断（防漫游扣费）" if (action == "hangup" or action == "auto_hangup") else action
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        content_md = (
            f"### 📞 未接来电提醒 (SimSync)\n"
            f"> **来电号码:** <font color=\"warning\">{caller}</font>\n"
            f"> **发生时间:** {now_str}\n"
            f"> **处理动作:** {action_desc}\n"
        )
        wh = custom_webhook or self.wecom_webhook
        uids = custom_uids if custom_uids is not None else self.wxpusher_uids
        if self.mode == "wecom_bot" and wh:
            self._send_wecom_markdown(content_md, target_webhook=wh)
        elif self.mode == "wxpusher" and self.wxpusher_token and uids:
            self._send_wxpusher(title, content_md, target_uids=uids)

    def _dispatch(self, title: str, markdown_content: str, text_content: str):
        if self.mode == "wecom_bot" and self.wecom_webhook:
            self._send_wecom_markdown(markdown_content)
        elif self.mode == "wxpusher" and self.wxpusher_token and self.wxpusher_uids:
            self._send_wxpusher(title, markdown_content)

    def _send_wecom_markdown(self, content: str, target_webhook: str = ""):
        wh = target_webhook or self.wecom_webhook
        if not wh:
            return
        try:
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
            resp = requests.post(wh, json=payload, timeout=8)
            if resp.status_code == 200 and resp.json().get("errcode") == 0:
                logger.info("企业微信机器人消息发送成功")
            else:
                logger.warning(f"企业微信机器人发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"企业微信机器人异常: {e}")

    def _send_wxpusher(self, summary: str, content: str, target_uids: List[str] = None):
        uids = target_uids if target_uids is not None else self.wxpusher_uids
        if not self.wxpusher_token or not uids:
            return
        try:
            url = "https://wxpusher.zjiecode.com/api/send/message"
            payload = {
                "appToken": self.wxpusher_token,
                "content": content,
                "summary": summary[:90],  # 最多100字摘要
                "contentType": 3,  # 3 表示 markdown
                "uids": uids,
            }
            resp = requests.post(url, json=payload, timeout=8)
            data = resp.json()
            if data.get("code") == 1000:
                logger.info("WxPusher 微信消息发送成功")
            else:
                logger.warning(f"WxPusher 发送失败: {data}")
        except Exception as e:
            logger.error(f"WxPusher 异常: {e}")
            if resp.status_code == 200 and resp.json().get("errcode") == 0:
                logger.info("企业微信机器人消息发送成功")
            else:
                logger.warning(f"企业微信机器人发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"企业微信机器人异常: {e}")

    def _send_wxpusher(self, summary: str, content: str):
        try:
            url = "https://wxpusher.zjiecode.com/api/send/message"
            payload = {
                "appToken": self.wxpusher_token,
                "content": content,
                "summary": summary[:90],  # 最多100字摘要
                "contentType": 3,  # 3 表示 markdown
                "uids": self.wxpusher_uids,
            }
            resp = requests.post(url, json=payload, timeout=8)
            data = resp.json()
            if data.get("code") == 1000:
                logger.info("WxPusher 微信消息发送成功")
            else:
                logger.warning(f"WxPusher 发送失败: {data}")
        except Exception as e:
            logger.error(f"WxPusher 异常: {e}")
