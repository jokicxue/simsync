import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Tuple

from simsync.config import NotificationConfig
from simsync.notifier.feishu import FeishuNotifier
from simsync.notifier.wechat import WechatNotifier
from simsync.notifier.email_push import EmailNotifier
from simsync.notifier.dingtalk import DingTalkNotifier
from simsync.notifier.telegram import TelegramNotifier
from simsync.notifier.custom_webhook import CustomWebhookNotifier

logger = logging.getLogger("simsync.notifier")


class NotificationManager:
    def __init__(self, config: NotificationConfig):
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="notifier")

        self._setup_channels(config)

    def _setup_channels(self, config: NotificationConfig):
        self.config = config

        # 飞书
        self.feishu = None
        if config.feishu.enabled:
            self.feishu = FeishuNotifier(
                webhook_url=config.feishu.webhook_url,
                bitable_config=config.feishu.bitable.model_dump(),
            )

        # 微信 (企业微信机器人 / WxPusher)
        self.wechat = None
        if config.wechat.enabled:
            self.wechat = WechatNotifier(
                mode=config.wechat.mode,
                wecom_webhook=config.wechat.wecom_bot.webhook_url,
                wxpusher_token=config.wechat.wxpusher.app_token,
                wxpusher_uids=config.wechat.wxpusher.uids,
            )

        # 邮件
        self.email = None
        if config.email.enabled:
            self.email = EmailNotifier(
                smtp_host=config.email.smtp_host,
                smtp_port=config.email.smtp_port,
                use_ssl=config.email.use_ssl,
                username=config.email.username,
                password=config.email.password,
                from_addr=config.email.from_addr,
                to_addrs=config.email.to_addrs,
            )

        # 钉钉
        self.dingtalk = None
        if config.dingtalk.enabled:
            self.dingtalk = DingTalkNotifier(
                webhook_url=config.dingtalk.webhook_url,
                secret=config.dingtalk.secret,
            )

        # Telegram
        self.telegram = None
        if config.telegram.enabled and config.telegram.bot_token:
            self.telegram = TelegramNotifier(
                bot_token=config.telegram.bot_token,
                chat_id=config.telegram.chat_id,
                proxy=config.telegram.proxy,
            )

        # 自定义 Webhook
        self.webhook = None
        if config.webhook.enabled:
            self.webhook = CustomWebhookNotifier(
                url=config.webhook.url,
                method=config.webhook.method,
            )

    def update_config(self, new_config: NotificationConfig):
        """动态更新推送配置，实时生效"""
        self._setup_channels(new_config)
        logger.info("推送通道配置已在内存中动态重载")


    def dispatch_sms(self, sms: Dict[str, Any]):
        """异步并发推送短信到所有启用的通道"""
        if self.feishu:
            self.executor.submit(self.feishu.send_sms_notification, sms)
        if self.wechat:
            self.executor.submit(self.wechat.send_sms_notification, sms)
        if self.email:
            self.executor.submit(self.email.send_sms_notification, sms)
        if self.dingtalk:
            self.executor.submit(self.dingtalk.send_sms_notification, sms)
        if self.telegram:
            self.executor.submit(self.telegram.send_sms_notification, sms)
        if self.webhook:
            self.executor.submit(self.webhook.send_sms_notification, sms)

    def dispatch_call(self, caller: str, action: str, receiver: str = ""):
        """异步并发推送未接来电到所有启用的通道"""
        cfg = self.config

        # 飞书
        if self.feishu and cfg.feishu.call_enabled:
            custom_url = cfg.feishu.call_webhook_url if cfg.feishu.call_use_custom else ""
            bitable_call_cfg = cfg.feishu.bitable.model_dump() if cfg.feishu.bitable.call_enabled else None
            self.executor.submit(self.feishu.send_call_notification, caller, action, custom_url, bitable_call_cfg, receiver)

        # 微信
        if self.wechat and cfg.wechat.call_enabled:
            custom_wh = cfg.wechat.call_wecom_webhook_url if cfg.wechat.call_use_custom else ""
            custom_uids = cfg.wechat.call_wxpusher_uids if cfg.wechat.call_use_custom else None
            self.executor.submit(self.wechat.send_call_notification, caller, action, custom_wh, custom_uids)

        # 邮件
        if self.email and cfg.email.call_enabled:
            custom_to = cfg.email.call_to_addrs if cfg.email.call_use_custom else None
            self.executor.submit(self.email.send_call_notification, caller, action, custom_to)

        # 钉钉
        if self.dingtalk and cfg.dingtalk.call_enabled:
            custom_url = cfg.dingtalk.call_webhook_url if cfg.dingtalk.call_use_custom else ""
            custom_sec = cfg.dingtalk.call_secret if cfg.dingtalk.call_use_custom else ""
            self.executor.submit(self.dingtalk.send_call_notification, caller, action, custom_url, custom_sec)

        # Telegram
        if self.telegram and cfg.telegram.call_enabled:
            custom_chat = cfg.telegram.call_chat_id if cfg.telegram.call_use_custom else ""
            self.executor.submit(self.telegram.send_call_notification, caller, action, custom_chat)

        # 自定义 Webhook
        if self.webhook and cfg.webhook.call_enabled:
            custom_url = cfg.webhook.call_url if cfg.webhook.call_use_custom else ""
            custom_method = cfg.webhook.call_method if cfg.webhook.call_use_custom else ""
            self.executor.submit(self.webhook.send_call_notification, caller, action, custom_url, custom_method)

    def test_channel(self, channel_name: str, event_type: str = "sms") -> Dict[str, Any]:
        """在线测试指定渠道的推送连通性 (支持 sms 或 call)"""
        sample_sms = {
            "sender": "+61412345678",
            "content": "【SimSync 测试】这是一条来自 SimSync 控制中心的推送通道连通性测试消息。",
            "readable_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": int(time.time() * 1000),
            "smsc": "+61418706700",
        }
        test_caller = "+61412345678"
        test_action = "拦截 (测试呼叫通知)"

        channel = channel_name.lower().strip()
        cfg = self.config
        try:
            if channel == "feishu":
                if not self.feishu:
                    return {"success": False, "error": "飞书推送未启用"}
                if event_type == "call":
                    custom_url = cfg.feishu.call_webhook_url if cfg.feishu.call_use_custom else ""
                    bitable_call_cfg = cfg.feishu.bitable.model_dump() if cfg.feishu.bitable.call_enabled else None
                    self.feishu.send_call_notification(test_caller, test_action, custom_url, bitable_call_cfg)
                    return {"success": True, "message": "飞书来电提醒测试消息已发送"}
                else:
                    self.feishu.send_sms_notification(sample_sms)
                    return {"success": True, "message": "飞书短信测试消息已发送"}

            elif channel in ("wechat", "wecom"):
                if not self.wechat:
                    return {"success": False, "error": "微信推送未启用"}
                if event_type == "call":
                    custom_wh = cfg.wechat.call_wecom_webhook_url if cfg.wechat.call_use_custom else ""
                    custom_uids = cfg.wechat.call_wxpusher_uids if cfg.wechat.call_use_custom else None
                    self.wechat.send_call_notification(test_caller, test_action, custom_wh, custom_uids)
                    return {"success": True, "message": "微信来电提醒测试消息已发送"}
                else:
                    self.wechat.send_sms_notification(sample_sms)
                    return {"success": True, "message": "微信/企业微信短信测试消息已发送"}

            elif channel == "email":
                if not self.email:
                    return {"success": False, "error": "邮件推送未启用或未配置 SMTP"}
                if event_type == "call":
                    custom_to = cfg.email.call_to_addrs if cfg.email.call_use_custom else None
                    self.email.send_call_notification(test_caller, test_action, custom_to)
                    return {"success": True, "message": "测试来电提醒邮件已发送"}
                else:
                    self.email.send_sms_notification(sample_sms)
                    return {"success": True, "message": "测试短信邮件已发送"}

            elif channel == "dingtalk":
                if not self.dingtalk:
                    return {"success": False, "error": "钉钉推送未启用"}
                if event_type == "call":
                    custom_url = cfg.dingtalk.call_webhook_url if cfg.dingtalk.call_use_custom else ""
                    custom_sec = cfg.dingtalk.call_secret if cfg.dingtalk.call_use_custom else ""
                    self.dingtalk.send_call_notification(test_caller, test_action, custom_url, custom_sec)
                    return {"success": True, "message": "钉钉来电提醒测试消息已发送"}
                else:
                    self.dingtalk.send_sms_notification(sample_sms)
                    return {"success": True, "message": "钉钉短信测试消息已发送"}

            elif channel == "telegram":
                if not self.telegram:
                    return {"success": False, "error": "Telegram 推送未启用或未配置 Bot Token"}
                if event_type == "call":
                    custom_chat = cfg.telegram.call_chat_id if cfg.telegram.call_use_custom else ""
                    self.telegram.send_call_notification(test_caller, test_action, custom_chat)
                    return {"success": True, "message": "Telegram 来电提醒测试消息已发送"}
                else:
                    self.telegram.send_sms_notification(sample_sms)
                    return {"success": True, "message": "Telegram 短信测试消息已发送"}

            elif channel == "webhook":
                if not self.webhook:
                    return {"success": False, "error": "自定义 Webhook 未启用"}
                if event_type == "call":
                    custom_url = cfg.webhook.call_url if cfg.webhook.call_use_custom else ""
                    custom_method = cfg.webhook.call_method if cfg.webhook.call_use_custom else ""
                    self.webhook.send_call_notification(test_caller, test_action, custom_url, custom_method)
                    return {"success": True, "message": "自定义 Webhook 来电测试触发完成"}
                else:
                    self.webhook.send_sms_notification(sample_sms)
                    return {"success": True, "message": "自定义 Webhook 短信测试触发完成"}

            else:
                return {"success": False, "error": f"未知的推送通道: {channel_name}"}

        except Exception as e:
            logger.error(f"测试推送通道 {channel_name} 异常: {e}")
            return {"success": False, "error": str(e)}

    def sync_feishu_bitable(self, db, phone_number: str = "") -> Dict[str, Any]:
        """调用飞书模块执行与飞书多维表格的比对与补录"""
        if not self.feishu:
            return {"success": False, "message": "飞书推送通道未启用"}
        return self.feishu.sync_sms_from_db(db, phone_number)

