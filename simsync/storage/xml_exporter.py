import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any


def generate_sms_backup_xml(messages: List[Dict[str, Any]]) -> str:
    """
    生成兼容 Android SMS Backup & Restore 标准格式的 XML 字符串
    
    格式规范:
    <smses count="N" backup_date="TIMESTAMP">
      <sms protocol="0" address="+61..." date="MS" type="1" subject="null" body="..." 
           toa="null" sc_toa="null" service_center="..." read="1" status="-1" locked="0" 
           date_sent="0" sub_id="1" readable_date="..." contact_name="(Unknown)" />
    </smses>
    """
    now_ms = int(datetime.now().timestamp() * 1000)
    
    root = ET.Element("smses")
    root.set("count", str(len(messages)))
    root.set("backup_set", f"simsync-{now_ms}")
    root.set("backup_date", str(now_ms))

    for msg in messages:
        sms = ET.SubElement(root, "sms")
        sms.set("protocol", "0")
        sms.set("address", str(msg.get("sender", "")))
        sms.set("date", str(msg.get("timestamp", now_ms)))
        sms.set("type", "1")  # 1 = 接收到的短信 (Inbox)
        sms.set("subject", "null")
        sms.set("body", str(msg.get("content", "")))
        sms.set("toa", "null")
        sms.set("sc_toa", "null")
        sms.set("service_center", str(msg.get("smsc") or "null"))
        sms.set("read", "1")
        sms.set("status", "-1")
        sms.set("locked", "0")
        sms.set("date_sent", "0")
        sms.set("sub_id", "1")
        sms.set("readable_date", str(msg.get("readable_date", "")))
        sms.set("contact_name", "(Unknown)")

    # 生成 XML 声明和注释
    xml_declaration = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
    comment = "<!-- File Created By SimSync for SMS Backup & Restore -->\n"
    xml_content = ET.tostring(root, encoding="utf-8").decode("utf-8")

    return xml_declaration + comment + xml_content


def generate_call_logs_backup_xml(calls: List[Dict[str, Any]]) -> str:
    """
    生成兼容 Android SMS Backup & Restore 标准格式的通话记录 XML 字符串
    
    格式规范:
    <calls count="N" backup_set="simsync-calls-TIMESTAMP" backup_date="TIMESTAMP">
      <call number="+61..." duration="0" date="MS" type="5" presentation="1" 
            readable_date="..." contact_name="(Unknown)" />
    </calls>
    """
    now_ms = int(datetime.now().timestamp() * 1000)
    
    root = ET.Element("calls")
    root.set("count", str(len(calls)))
    root.set("backup_set", f"simsync-calls-{now_ms}")
    root.set("backup_date", str(now_ms))

    for c in calls:
        call = ET.SubElement(root, "call")
        call.set("number", str(c.get("caller", "")))
        call.set("duration", "0")
        call.set("date", str(c.get("timestamp", now_ms)))
        # type 规范: 1=来电已接, 2=去电, 3=未接, 5=拒接/黑名单/自动拦截挂断
        call.set("type", "5" if c.get("action") == "auto_hangup" or c.get("action") == "hangup" else "3")
        call.set("presentation", "1")
        call.set("readable_date", str(c.get("readable_date", "")))
        call.set("contact_name", "(Unknown)")

    xml_declaration = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
    comment = "<!-- File Created By SimSync for Call Logs Backup & Restore -->\n"
    xml_content = ET.tostring(root, encoding="utf-8").decode("utf-8")

    return xml_declaration + comment + xml_content
