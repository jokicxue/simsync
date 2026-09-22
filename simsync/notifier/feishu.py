import logging
import time
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("simsync.notifier.feishu")


class FeishuNotifier:
    def __init__(self, webhook_url: str = "", bitable_config: Optional[Dict[str, Any]] = None):
        self.webhook_url = webhook_url
        self.bitable_config = bitable_config or {}

        self._tenant_token = ""
        self._token_expires_at = 0

    def send_sms_notification(self, sms: Dict[str, Any]):
        """发送短信通知：同时尝试 Webhook 和 多维表格"""
        if self.webhook_url:
            self._send_webhook_card(
                title="📩 收到新短信 (SimSync)",
                color="blue",
                fields=[
                    {"key": "发件人", "value": sms.get("sender", "未知")},
                    {"key": "时间", "value": sms.get("readable_date", "")},
                    {"key": "短信内容", "value": sms.get("content", "")},
                ],
            )

        if self.bitable_config.get("enabled"):
            self._save_to_bitable(sms)

    def send_call_notification(
        self,
        caller: str,
        action: str,
        custom_webhook_url: str = "",
        bitable_call_cfg: Optional[Dict[str, Any]] = None,
        receiver: str = "",
    ):
        """发送未接来电通知：支持独立 Webhook 和 多维表格独立工作表"""
        wh_url = custom_webhook_url or self.webhook_url
        if wh_url:
            action_desc = "已自动挂断（防漫游扣费）" if (action == "hangup" or action == "auto_hangup") else action
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            fields = [
                {"key": "来电号码", "value": caller},
                {"key": "发生时间", "value": now_str},
                {"key": "处理状态", "value": action_desc},
            ]
            if receiver:
                fields.insert(0, {"key": "本机号码", "value": receiver})
            self._send_webhook_card(
                title="📞 未接来电提醒 (SimSync)",
                color="red",
                fields=fields,
                target_url=wh_url,
            )

        b_cfg = bitable_call_cfg or self.bitable_config
        if b_cfg.get("call_enabled", False):
            self._save_call_to_bitable(caller, action, b_cfg, receiver=receiver)

    def _send_webhook_card(self, title: str, color: str, fields: list, target_url: str = ""):
        """发送飞书交互式消息卡片"""
        post_url = target_url or self.webhook_url
        if not post_url:
            return
        try:
            elements = []
            for item in fields:
                elements.append(
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{item['key']}:** {item['value']}",
                        },
                    }
                )

            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": color,
                    },
                    "elements": elements,
                },
            }

            resp = requests.post(post_url, json=payload, timeout=8)
            if resp.status_code == 200 and resp.json().get("code") == 0:
                logger.info("飞书 Webhook 消息发送成功")
            else:
                logger.warning(f"飞书 Webhook 消息发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"飞书 Webhook 异常: {e}")

    def _get_tenant_access_token(self) -> Optional[str]:
        """获取飞书自建应用的 tenant_access_token"""
        now = time.time()
        if self._tenant_token and now < self._token_expires_at - 60:
            return self._tenant_token

        app_id = self.bitable_config.get("app_id")
        app_secret = self.bitable_config.get("app_secret")
        if not app_id or not app_secret:
            return None

        try:
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=8)
            data = resp.json()
            if data.get("code") == 0:
                self._tenant_token = data.get("tenant_access_token")
                self._token_expires_at = now + data.get("expire", 7200)
                return self._tenant_token
            else:
                logger.error(f"获取飞书 token 失败: {data}")
        except Exception as e:
            logger.error(f"请求飞书 token 异常: {e}")
        return None

    def _get_real_app_token(self, token: str, app_token_or_wiki: str) -> str:
        """如果用户提供的是 Wiki 节点 token，自动调用飞书 API 转换为多维表格的真实 app_token (bascn...)"""
        if app_token_or_wiki.startswith("bascn"):
            return app_token_or_wiki

        try:
            url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={app_token_or_wiki}&obj_type=wiki"
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, headers=headers, timeout=8)
            data = resp.json()
            if data.get("code") == 0:
                obj_token = data.get("data", {}).get("node", {}).get("obj_token")
                if obj_token:
                    logger.info(f"成功将飞书 Wiki 节点解析为多维表格 app_token: {obj_token}")
                    return obj_token
        except Exception as e:
            logger.warning(f"解析飞书 Wiki 节点 token 异常: {e}")

        return app_token_or_wiki

    def _save_to_bitable(self, sms: Dict[str, Any]):
        """将短信自动插入飞书多维表格作为云端永久备份"""
        token = self._get_tenant_access_token()
        if not token:
            return

        raw_app_token = self.bitable_config.get("app_token")
        table_id = self.bitable_config.get("table_id")
        if not raw_app_token or not table_id:
            return

        # 智能解析真实的 base app_token (支持 wiki 格式)
        app_token = self._get_real_app_token(token, raw_app_token)

        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        record_data = {
            "fields": {
                "本机号码": sms.get("receiver", ""),
                "发件人": sms.get("sender", ""),
                "短信内容": sms.get("content", ""),
                "接收时间": sms.get("readable_date", ""),
                "短信中心": sms.get("smsc", ""),
            }
        }

        try:
            resp = requests.post(url, headers=headers, json=record_data, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                logger.info("短信已自动同步备份到飞书多维表格 (云端持久化)")
            else:
                logger.warning(f"写入飞书多维表格失败: {data}")
        except Exception as e:
            logger.error(f"写入飞书多维表格异常: {e}")

    def _save_call_to_bitable(
        self,
        caller: str,
        action: str,
        bitable_cfg: Optional[Dict[str, Any]] = None,
        receiver: str = "",
    ):
        """将来电记录自动插入飞书多维表格作为云端永久备份"""
        token = self._get_tenant_access_token()
        if not token:
            return

        cfg = bitable_cfg or self.bitable_config
        call_mode = cfg.get("call_mode", "same_base")
        if call_mode == "same_base":
            raw_app_token = cfg.get("app_token")
            table_id = cfg.get("call_table_id") or cfg.get("table_id")
        elif call_mode == "custom":
            raw_app_token = cfg.get("call_app_token") or cfg.get("app_token")
            table_id = cfg.get("call_table_id")
        else:  # same_table
            raw_app_token = cfg.get("app_token")
            table_id = cfg.get("table_id")

        if not raw_app_token or not table_id:
            return

        app_token = self._get_real_app_token(token, raw_app_token)
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        action_desc = "已自动挂断（防漫游扣费）" if (action == "hangup" or action == "auto_hangup") else action

        if table_id == cfg.get("table_id") and call_mode == "same_table":
            fields = {
                "本机号码": receiver,
                "发件人": caller,
                "短信内容": f"[来电提醒] {action_desc}",
                "接收时间": now_str,
                "短信中心": "语音呼叫",
            }
        else:
            fields = {
                "本机号码": receiver,
                "来电号码": caller,
                "处理动作": action_desc,
                "发生时间": now_str,
            }

        try:
            resp = requests.post(url, headers=headers, json={"fields": fields}, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                logger.info("来电记录已自动同步到飞书多维表格")
            else:
                logger.warning(f"写入飞书多维表格来电失败: {data}")
        except Exception as e:
            logger.error(f"写入飞书多维表格来电异常: {e}")

    def sync_sms_from_db(self, db, phone_number: str = "") -> Dict[str, Any]:
        """
        与飞书多维表格比对并增量同步缺失的短信：
        1. 校验飞书配置与获取 token
        2. 分页拉取多维表格中已有的所有记录
        3. 过滤出属于当前卡号（phone_number）的记录，构建指纹库 (MD5)
        4. 从本地 SQLite 提取属于该卡号的所有短信，计算指纹并找出缺失记录
        5. 调用飞书批量新增接口 (batch_create)，每批最多 500 条
        6. 返回比对与同步结果
        """
        import hashlib

        token = self._get_tenant_access_token()
        if not token:
            return {"success": False, "message": "无法获取飞书 tenant_access_token，请检查 App ID 与 App Secret 是否正确"}

        raw_app_token = self.bitable_config.get("app_token")
        table_id = self.bitable_config.get("table_id")
        if not raw_app_token or not table_id:
            return {"success": False, "message": "飞书多维表格未配置 app_token 或 table_id"}

        app_token = self._get_real_app_token(token, raw_app_token)
        target_phone = (phone_number or "").strip()

        # 1. 分页拉取多维表格中的所有已有记录
        existing_records = []
        page_token = ""
        has_more = True

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        base_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

        while has_more:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            try:
                resp = requests.get(base_url, headers=headers, params=params, timeout=15)
                data = resp.json()
                if data.get("code") != 0:
                    return {"success": False, "message": f"拉取飞书多维表格记录失败: {data.get('msg', data)}"}
                items = data.get("data", {}).get("items", [])
                existing_records.extend(items)
                has_more = data.get("data", {}).get("has_more", False)
                page_token = data.get("data", {}).get("page_token", "")
            except Exception as e:
                return {"success": False, "message": f"请求飞书多维表格异常: {e}"}

        # 2. 构建当前手机号在飞书上的指纹库
        def make_fingerprint(phone: str, sender: str, readable_date: str, content: str) -> str:
            norm_str = f"{(phone or '').strip()}|{(sender or '').strip()}|{(readable_date or '').strip()}|{(content or '').strip()}"
            return hashlib.md5(norm_str.encode("utf-8")).hexdigest()

        feishu_fingerprints = set()
        feishu_phone_matched = 0

        for rec in existing_records:
            f = rec.get("fields", {})
            rec_phone = str(f.get("本机号码") or f.get("接收号码") or f.get("卡号") or "").strip()
            rec_sender = str(f.get("发件人") or f.get("来电号码") or "").strip()
            rec_date = str(f.get("接收时间") or f.get("发生时间") or "").strip()
            rec_content = str(f.get("短信内容") or f.get("处理动作") or "").strip()

            # 若指定了 target_phone 且记录填写了号码，不匹配则跳过（避免与别的卡号混淆）
            if target_phone and rec_phone and rec_phone != target_phone:
                continue

            feishu_phone_matched += 1
            fp = make_fingerprint(rec_phone or target_phone, rec_sender, rec_date, rec_content)
            feishu_fingerprints.add(fp)

        # 3. 从本地数据库读取属于该号码的全部短信
        local_messages = db.get_sms_by_receiver(target_phone)

        missing_records = []
        for sms in local_messages:
            sms_phone = (sms.get("receiver") or target_phone).strip()
            sms_sender = str(sms.get("sender") or "").strip()
            sms_date = str(sms.get("readable_date") or "").strip()
            sms_content = str(sms.get("content") or "").strip()

            fp = make_fingerprint(sms_phone, sms_sender, sms_date, sms_content)
            if fp not in feishu_fingerprints:
                missing_records.append({
                    "fields": {
                        "本机号码": sms_phone,
                        "发件人": sms_sender,
                        "短信内容": sms_content,
                        "接收时间": sms_date,
                        "短信中心": str(sms.get("smsc") or "").strip(),
                    }
                })
                feishu_fingerprints.add(fp)

        # 4. 批量写入缺失记录到飞书 (每批最多 500 条)
        synced_count = 0
        batch_size = 500
        batch_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"

        for i in range(0, len(missing_records), batch_size):
            chunk = missing_records[i : i + batch_size]
            payload = {"records": chunk}
            try:
                resp = requests.post(batch_url, headers=headers, json=payload, timeout=20)
                data = resp.json()
                if data.get("code") == 0:
                    synced_count += len(chunk)
                else:
                    logger.warning(f"飞书批量新增记录部分失败: {data}")
            except Exception as e:
                logger.error(f"飞书批量新增记录异常: {e}")
                break

        msg = (
            f"同步完成：本地共 {len(local_messages)} 条短信，"
            f"飞书表格已有 {feishu_phone_matched} 条对应记录，"
            f"本次成功补录 {synced_count} 条缺失短信。"
        ) if synced_count > 0 else (
            f"比对完成：本地共 {len(local_messages)} 条短信已全部存在于飞书多维表格中，无需补录。"
        )

        return {
            "success": True,
            "phone_number": target_phone,
            "total_local": len(local_messages),
            "feishu_existing": feishu_phone_matched,
            "synced_new": synced_count,
            "message": msg,
        }

