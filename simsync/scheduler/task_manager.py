import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from simsync.storage.database import Database
from simsync.modem.at_client import ModemClient
from simsync.notifier.manager import NotificationManager

logger = logging.getLogger("simsync.scheduler")


class TaskManager:
    def __init__(self, db: Database, modem: ModemClient, notifier: Optional[NotificationManager] = None):
        self.db = db
        self.modem = modem
        self.notifier = notifier
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._in_scheduled_flight: bool = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="task_scheduler")
        self._thread.start()
        logger.info("定时保号任务与飞行计划调度器已启动")

    def stop(self):
        self._running = False

    def _check_flight_schedules(self):
        """检查定时飞行模式计划"""
        try:
            schedules = self.db.get_flight_schedules()
            active_schedules = [s for s in schedules if s.get("enabled")]
            if not active_schedules:
                if self._in_scheduled_flight:
                    self._in_scheduled_flight = False
                return

            now = datetime.now()
            now_weekday = str(now.isoweekday())  # 1 (Monday) to 7 (Sunday)
            now_hm = now.strftime("%H:%M")

            should_be_in_flight = False
            matching_sched_name = ""

            for sched in active_schedules:
                days = [d.strip() for d in sched.get("days_of_week", "1,2,3,4,5,6,7").split(",")]
                if now_weekday not in days:
                    continue

                start = sched.get("start_time", "").strip()
                end = sched.get("end_time", "").strip()
                if not start or not end:
                    continue

                is_active = False
                if start <= end:
                    is_active = (start <= now_hm < end)
                else:
                    # 跨午夜 (例如 23:00 到 次日 07:00)
                    is_active = (now_hm >= start or now_hm < end)

                if is_active:
                    should_be_in_flight = True
                    matching_sched_name = sched.get("name", "定时计划")
                    break

            if should_be_in_flight:
                if not self._in_scheduled_flight:
                    self._in_scheduled_flight = True
                    if not self.modem.is_flight_mode:
                        logger.info(f"触发定时飞行计划 [{matching_sched_name}]，正在将模块置入飞行模式...")
                        self.modem.set_flight_mode(True, f"定时计划: {matching_sched_name}")
            else:
                if self._in_scheduled_flight:
                    self._in_scheduled_flight = False
                    if self.modem.is_flight_mode:
                        logger.info("定时飞行计划时段已结束，正在唤醒蜂窝模块...")
                        self.modem.set_flight_mode(False, "定时计划结束唤醒")

        except Exception as e:
            logger.error(f"检查定时飞行计划异常: {e}")

    def _worker_loop(self):
        """定期检查定时保号任务与定时飞行计划"""
        while self._running:
            try:
                # 1. 检查定时保号短信
                due_tasks = self.db.get_due_tasks()
                for task in due_tasks:
                    logger.info(f"触发定时保号任务: [{task['id']}] {task['name']} -> {task['recipient']}")
                    self.execute_task(task)

                # 2. 检查定时飞行计划
                self._check_flight_schedules()
            except Exception as e:
                logger.error(f"定时任务轮询异常: {e}")

            # 睡眠 30 秒
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """执行特定任务并更新状态"""
        task_id = task["id"]
        recipient = task["recipient"]
        content = task["content"]
        interval_days = task.get("interval_days", 90)

        logger.info(f"开始执行保号任务 [{task.get('name', task_id)}]: 向 {recipient} 发送 '{content}'")
        res = self.modem.send_sms(recipient=recipient, content=content)

        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if res.get("success"):
            next_run = (now + timedelta(days=interval_days)).strftime("%Y-%m-%d %H:%M:%S")
            self.db.update_scheduled_task(
                task_id=task_id,
                last_run_at=now_str,
                next_run_at=next_run,
            )
            self.db.save_outbound_sms(recipient=recipient, content=content, status="sent")

            # 推送通知
            if self.notifier:
                notice = {
                    "sender": f"保号任务: {task.get('name')}",
                    "content": f"✅ 保号短信发送成功！\n- 目标号码: {recipient}\n- 发送内容: {content}\n- 下次执行: {next_run}",
                    "readable_date": now_str,
                    "timestamp": int(now.timestamp() * 1000),
                }
                self.notifier.dispatch_sms(notice)

            return {
                "success": True,
                "message": f"保号短信已成功发送至 {recipient}，下次执行时间: {next_run}",
            }
        else:
            err = res.get("error", "未知错误")
            self.db.save_outbound_sms(recipient=recipient, content=content, status="failed")

            if self.notifier:
                notice = {
                    "sender": f"保号任务失败: {task.get('name')}",
                    "content": f"❌ 保号短信发送失败！\n- 目标号码: {recipient}\n- 错误原因: {err}\n- 请检查模块与网络状态！",
                    "readable_date": now_str,
                    "timestamp": int(now.timestamp() * 1000),
                }
                self.notifier.dispatch_sms(notice)

            return {
                "success": False,
                "error": f"保号短信发送失败: {err}",
            }

    def run_task_now(self, task_id: int) -> Dict[str, Any]:
        """手动立即执行某个保号任务"""
        tasks = self.db.get_scheduled_tasks()
        target = next((t for t in tasks if t["id"] == task_id), None)
        if not target:
            return {"success": False, "error": "未找到指定的任务"}
        return self.execute_task(target)
