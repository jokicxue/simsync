import argparse
import logging
import sys
import uvicorn

from simsync.config import load_config
from simsync.storage.database import Database
from simsync.storage.crypto import DataCipher
from simsync.notifier.manager import NotificationManager
from simsync.modem.at_client import ModemClient
from simsync.scheduler.task_manager import TaskManager
from simsync.web.app import create_app

# 配置日志 (同时输出至控制台与 data/simsync.log 文本文件)
log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    Path("data").mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        "data/simsync.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s"))
    log_handlers.append(file_handler)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger("simsync")


def main():
    parser = argparse.ArgumentParser(description="SimSync - 澳洲漫游卡短信接收与呼叫管理系统")
    parser.add_argument("-c", "--config", type=str, help="配置文件路径 (默认自动寻找 config.yaml)")
    args = parser.parse_args()

    logger.info("正在启动 SimSync 系统...")
    config = load_config(args.config)

    # 1. 初始化加密器与数据库 (实现短信静态数据 AES-256 加密存储)
    cipher = DataCipher(password=config.server.password or config.server.password_hash)
    db = Database(config.storage.db_path, cipher=cipher)
    logger.info(f"SQLite 数据库已就绪 (数据静态加密已启用): {config.storage.db_path}")

    # 2. 初始化多渠道通知管理器
    notifier = NotificationManager(config.notifications)
    logger.info("多渠道推送管理器已就绪 (飞书/微信/邮件/钉钉/Telegram/Webhook)")

    # 3. 回调函数定义
    def handle_sms(sms_data: dict):
        logger.info(f"收到短信，开始持久化与多通道转发: {sms_data.get('sender')}")
        current_phone = ""
        try:
            current_phone = getattr(modem, "phone_number", None) or config.modem.phone_number or ""
        except Exception:
            current_phone = config.modem.phone_number or ""
        sms_data["receiver"] = current_phone

        db.save_sms(
            sender=sms_data.get("sender", ""),
            content=sms_data.get("content", ""),
            timestamp_ms=sms_data.get("timestamp"),
            readable_date=sms_data.get("readable_date"),
            smsc=sms_data.get("smsc"),
            raw_pdu=sms_data.get("raw_pdu"),
            direction="inbound",
            status="received",
            receiver=current_phone,
        )
        notifier.dispatch_sms(sms_data)

    def handle_call(caller: str, action: str):
        logger.info(f"收到来电事件，开始记录与告警推送: {caller} | 动作: {action}")
        current_phone = ""
        try:
            current_phone = getattr(modem, "phone_number", None) or config.modem.phone_number or ""
        except Exception:
            current_phone = config.modem.phone_number or ""
        db.save_call(caller=caller, action=action)
        notifier.dispatch_call(caller=caller, action=action, receiver=current_phone)

    def handle_flight_change(event: str, reason: str):
        logger.info(f"飞行模式事件: {event} | 原因: {reason}")
        db.save_flight_log(event=event, reason=reason)

    # 4. 初始化串口客户端 (支持自动探测、飞行模式与外发短信)
    modem = ModemClient(
        port=config.modem.port,
        baudrate=config.modem.baudrate,
        timeout=config.modem.timeout,
        auto_hangup=config.modem.auto_hangup_calls,
        phone_number=config.modem.phone_number,
        auto_flight=config.auto_flight,
        on_sms_received=handle_sms,
        on_call_received=handle_call,
        on_flight_changed=handle_flight_change,
    )
    modem.start()

    # 5. 初始化并启动定时保号任务管理器
    task_manager = TaskManager(db, modem, notifier)
    task_manager.start()

    # 6. 创建并运行 Web 控制台
    app = create_app(config, db, modem, notifier=notifier, task_manager=task_manager)
    logger.info(f"Web 控制台服务启动于: http://{config.server.host}:{config.server.port}")

    try:
        uvicorn.run(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level="info",
        )
    finally:
        logger.info("正在停止 SimSync 服务...")
        task_manager.stop()
        modem.stop()


if __name__ == "__main__":
    main()
