import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from simsync.storage.crypto import DataCipher


class Database:
    def __init__(self, db_path: str = "data/simsync.db", cipher: Optional[DataCipher] = None):
        self.db_path = db_path
        self.cipher = cipher or DataCipher()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. 短信表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS sms_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,       -- 毫秒时间戳 (SMS Backup & Restore 标准)
                readable_date TEXT NOT NULL,     -- 人类可读格式
                smsc TEXT,                       -- 短信中心号码
                raw_pdu TEXT,                    -- 原始 PDU
                direction TEXT DEFAULT 'inbound', -- inbound (收信) 或 outbound (发信)
                status TEXT DEFAULT 'received',  -- received, sent, failed
                receiver TEXT DEFAULT '',        -- 本机号码 / 接收卡号
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 兼容旧表升级：检查并添加 direction, status 和 receiver 列
            cursor.execute("PRAGMA table_info(sms_messages)")
            existing_cols = [row["name"] for row in cursor.fetchall()]
            if "direction" not in existing_cols:
                cursor.execute("ALTER TABLE sms_messages ADD COLUMN direction TEXT DEFAULT 'inbound'")
            if "status" not in existing_cols:
                cursor.execute("ALTER TABLE sms_messages ADD COLUMN status TEXT DEFAULT 'received'")
            if "receiver" not in existing_cols:
                cursor.execute("ALTER TABLE sms_messages ADD COLUMN receiver TEXT DEFAULT ''")

            # 2. 来电记录表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller TEXT NOT NULL,
                action TEXT NOT NULL,            -- hangup, forwarded, missed
                timestamp INTEGER NOT NULL,
                readable_date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 3. 定时保号任务表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                recipient TEXT NOT NULL,
                content TEXT NOT NULL,
                interval_days INTEGER NOT NULL DEFAULT 90,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 4. 定时飞行计划表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS flight_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,         -- e.g. "23:00" (进入休眠)
                end_time TEXT NOT NULL,           -- e.g. "07:00" (唤醒射频)
                days_of_week TEXT DEFAULT '1,2,3,4,5,6,7',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 5. 飞行模式变迁日志表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS flight_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,              -- enter_flight, wake_up
                reason TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                readable_date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # 6. 系统元数据表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()

    # ==================== 短信读写与会话 ====================

    def save_sms(
        self,
        sender: str,
        content: str,
        timestamp_ms: Optional[int] = None,
        readable_date: Optional[str] = None,
        smsc: Optional[str] = None,
        raw_pdu: Optional[str] = None,
        direction: str = "inbound",
        status: str = "received",
        receiver: Optional[str] = "",
    ) -> int:
        now = datetime.now()
        if timestamp_ms is None:
            timestamp_ms = int(now.timestamp() * 1000)
        if readable_date is None:
            readable_date = now.strftime("%Y-%m-%d %H:%M:%S")

        enc_sender = self.cipher.encrypt(sender)
        enc_content = self.cipher.encrypt(content)
        enc_pdu = self.cipher.encrypt(raw_pdu)
        enc_receiver = self.cipher.encrypt(receiver or "")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sms_messages (sender, content, timestamp, readable_date, smsc, raw_pdu, direction, status, receiver)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (enc_sender, enc_content, timestamp_ms, readable_date, smsc, enc_pdu, direction, status, enc_receiver),
            )
            return cursor.lastrowid

    def save_outbound_sms(
        self,
        recipient: str,
        content: str,
        status: str = "sent",
        timestamp_ms: Optional[int] = None,
        receiver: Optional[str] = "",
    ) -> int:
        """保存发送出去的短信记录"""
        now = datetime.now()
        if timestamp_ms is None:
            timestamp_ms = int(now.timestamp() * 1000)
        readable_date = now.strftime("%Y-%m-%d %H:%M:%S")

        return self.save_sms(
            sender=recipient,
            content=content,
            timestamp_ms=timestamp_ms,
            readable_date=readable_date,
            smsc="",
            raw_pdu="",
            direction="outbound",
            status=status,
            receiver=receiver,
        )

    def get_all_sms(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, sender, content, timestamp, readable_date, smsc, raw_pdu, direction, status, created_at, receiver
                FROM sms_messages
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d["sender"] = self.cipher.decrypt(d["sender"])
                d["content"] = self.cipher.decrypt(d["content"])
                if d.get("raw_pdu"):
                    d["raw_pdu"] = self.cipher.decrypt(d["raw_pdu"])
                if d.get("receiver"):
                    d["receiver"] = self.cipher.decrypt(d["receiver"])
                else:
                    d["receiver"] = ""
                results.append(d)
            return results

    def get_statistics(self) -> Dict[str, int]:
        """获取系统概览统计数字"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM sms_messages")
            total_sms = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM sms_messages WHERE direction = 'inbound' OR direction IS NULL")
            inbound_sms = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM sms_messages WHERE direction = 'outbound'")
            outbound_sms = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM call_logs")
            total_calls = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM scheduled_tasks")
            total_tasks = cursor.fetchone()["cnt"]

            return {
                "total_sms": total_sms,
                "inbound_sms": inbound_sms,
                "outbound_sms": outbound_sms,
                "total_calls": total_calls,
                "total_tasks": total_tasks,
            }

    def get_conversations(self) -> List[Dict[str, Any]]:

        """
        获取联系人会话列表，按最后联系时间降序排列
        返回: [
            {"phone": "+61412345678", "last_message": "...", "last_time": "2026-09-21 12:00:00", "count": 5},
            ...
        ]
        """
        # 读取全部短信并在内存中解密分组
        all_sms = self.get_all_sms(limit=1000)
        conv_map: Dict[str, Dict[str, Any]] = {}

        for sms in all_sms:
            phone = sms["sender"].strip()
            if not phone:
                phone = "未知号码"

            if phone not in conv_map:
                conv_map[phone] = {
                    "phone": phone,
                    "last_message": sms["content"],
                    "last_time": sms["readable_date"],
                    "timestamp": sms["timestamp"],
                    "count": 1,
                }
            else:
                conv_map[phone]["count"] += 1
                if sms["timestamp"] > conv_map[phone]["timestamp"]:
                    conv_map[phone]["last_message"] = sms["content"]
                    conv_map[phone]["last_time"] = sms["readable_date"]
                    conv_map[phone]["timestamp"] = sms["timestamp"]

        # 按最后时间倒序排列
        convs = list(conv_map.values())
        convs.sort(key=lambda x: x["timestamp"], reverse=True)
        return convs

    def get_conversation_messages(self, phone: str, limit: int = 200) -> List[Dict[str, Any]]:
        """获取与指定号码的往来对话消息（按时间正序排列）"""
        all_sms = self.get_all_sms(limit=limit * 2)
        filtered = [s for s in all_sms if s["sender"].strip() == phone.strip()]
        filtered.sort(key=lambda x: x["timestamp"])
        return filtered

    def get_sms_for_export(self) -> List[Dict[str, Any]]:
        """获取所有短信，按时间正序排列（用于导出 XML）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, sender, content, timestamp, readable_date, smsc, direction, status
                FROM sms_messages
                ORDER BY timestamp ASC
                """
            )
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d["sender"] = self.cipher.decrypt(d["sender"])
                d["content"] = self.cipher.decrypt(d["content"])
                results.append(d)
            return results

    # ==================== 来电记录 ====================

    def save_call(
        self,
        caller: str,
        action: str = "hangup",
        timestamp_ms: Optional[int] = None,
        readable_date: Optional[str] = None,
    ) -> int:
        now = datetime.now()
        if timestamp_ms is None:
            timestamp_ms = int(now.timestamp() * 1000)
        if readable_date is None:
            readable_date = now.strftime("%Y-%m-%d %H:%M:%S")

        enc_caller = self.cipher.encrypt(caller)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO call_logs (caller, action, timestamp, readable_date)
                VALUES (?, ?, ?, ?)
                """,
                (enc_caller, action, timestamp_ms, readable_date),
            )
            return cursor.lastrowid

    def get_all_calls(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, caller, action, timestamp, readable_date, created_at
                FROM call_logs
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d["caller"] = self.cipher.decrypt(d["caller"])
                results.append(d)
            return results

    def delete_call(self, call_id: int) -> bool:
        """删除单条来电记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM call_logs WHERE id = ?", (call_id,))
            return cursor.rowcount > 0

    def clear_all_calls(self) -> bool:
        """清空全部来电记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM call_logs")
            return True

    def get_calls_for_export(self) -> List[Dict[str, Any]]:
        """获取所有来电记录，按时间正序排列（用于导出 XML）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, caller, action, timestamp, readable_date, created_at
                FROM call_logs
                ORDER BY timestamp ASC
                """
            )
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d["caller"] = self.cipher.decrypt(d["caller"])
                results.append(d)
            return results

    # ==================== 定时飞行计划与事件日志 ====================

    def get_flight_schedules(self) -> List[Dict[str, Any]]:
        """获取所有定时飞行计划"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flight_schedules ORDER BY id ASC")
            return [dict(row) for row in cursor.fetchall()]

    def add_flight_schedule(
        self,
        name: str,
        start_time: str,
        end_time: str,
        days_of_week: str = "1,2,3,4,5,6,7",
    ) -> int:
        """添加定时飞行计划"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO flight_schedules (name, start_time, end_time, days_of_week, enabled)
                VALUES (?, ?, ?, ?, 1)
                """,
                (name, start_time, end_time, days_of_week),
            )
            return cursor.lastrowid

    def update_flight_schedule(self, schedule_id: int, **fields) -> bool:
        """更新定时飞行计划"""
        if not fields:
            return False
        set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values()) + [schedule_id]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE flight_schedules SET {set_clause} WHERE id = ?", values)
            return cursor.rowcount > 0

    def delete_flight_schedule(self, schedule_id: int) -> bool:
        """删除定时飞行计划"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM flight_schedules WHERE id = ?", (schedule_id,))
            return cursor.rowcount > 0

    def save_flight_log(
        self,
        event: str,
        reason: str,
        timestamp_ms: Optional[int] = None,
        readable_date: Optional[str] = None,
    ) -> int:
        """记录飞行模式切换事件日志"""
        now = datetime.now()
        if timestamp_ms is None:
            timestamp_ms = int(now.timestamp() * 1000)
        if readable_date is None:
            readable_date = now.strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO flight_logs (event, reason, timestamp, readable_date)
                VALUES (?, ?, ?, ?)
                """,
                (event, reason, timestamp_ms, readable_date),
            )
            return cursor.lastrowid

    def get_flight_logs(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """获取最近的飞行模式变迁日志"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, event, reason, timestamp, readable_date, created_at
                FROM flight_logs
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 定时保号任务 ====================

    def get_scheduled_tasks(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM scheduled_tasks ORDER BY id ASC")
            return [dict(row) for row in cursor.fetchall()]

    def add_scheduled_task(
        self,
        name: str,
        recipient: str,
        content: str,
        interval_days: int = 90,
    ) -> int:
        now = datetime.now()
        next_run = (now + timedelta(days=interval_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO scheduled_tasks (name, recipient, content, interval_days, enabled, next_run_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (name, recipient, content, interval_days, next_run),
            )
            return cursor.lastrowid

    def update_scheduled_task(self, task_id: int, **fields) -> bool:
        if not fields:
            return False
        set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values()) + [task_id]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?", values)
            return cursor.rowcount > 0

    def delete_scheduled_task(self, task_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    def get_due_tasks(self) -> List[Dict[str, Any]]:
        """获取当前已到期需要执行的定时任务"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                """,
                (now_str,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 元数据键值 ====================

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_meta WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO system_meta (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            conn.commit()

    def get_sms_by_receiver(self, receiver: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取指定本机号码收发的所有短信（按时间正序排列，便于同步与比对）。
        若 receiver 未提供或为空，则返回全部短信。
        对于未记录 receiver 的旧历史短信，也会包含在内并临时标注为 target_phone 以确保兼容性。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, sender, content, timestamp, readable_date, smsc, raw_pdu, direction, status, created_at, receiver
                FROM sms_messages
                ORDER BY timestamp ASC
                """
            )
            results = []
            target_phone = (receiver or "").strip()
            for row in cursor.fetchall():
                d = dict(row)
                d["sender"] = self.cipher.decrypt(d["sender"])
                d["content"] = self.cipher.decrypt(d["content"])
                if d.get("raw_pdu"):
                    d["raw_pdu"] = self.cipher.decrypt(d["raw_pdu"])
                dec_recv = self.cipher.decrypt(d.get("receiver") or "") if d.get("receiver") else ""
                d["receiver"] = dec_recv

                # 过滤逻辑：若指定了 target_phone，仅匹配 dec_recv == target_phone 或 历史空记录
                if target_phone:
                    if dec_recv and dec_recv != target_phone:
                        continue
                    if not dec_recv:
                        d["receiver"] = target_phone

                results.append(d)
            return results

    def save_config_backup(self, config_dict: dict):
        """将系统完整配置备份写入 SQLite system_meta 表"""
        import json
        self.set_meta("backup_config_json", json.dumps(config_dict, ensure_ascii=False))

    def load_config_backup(self) -> Optional[dict]:
        """从 SQLite system_meta 表读取系统完整配置备份"""
        import json
        raw = self.get_meta("backup_config_json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

