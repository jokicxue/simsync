import re
from typing import Dict, Any, Optional, Callable


class CallForwardController:
    """
    基于 3GPP TS 27.007 标准的呼叫转移 (Call Forwarding) 管理器
    利用 AT+CCFC 指令实现预设号码、查询、激活、停用
    """

    REASONS = {
        0: "无条件转移 (Unconditional)",
        1: "遇忙转移 (Busy)",
        2: "无应答转移 (No Reply)",
        3: "不可及转移 (Not Reachable)",
        4: "所有呼叫转移 (All Forwarding)",
        5: "所有条件转移 (All Conditional)",
    }

    def __init__(self, send_at_func: Callable[[str, float], str]):
        self.send_at = send_at_func

    def query(self, reason: int = 0) -> Dict[str, Any]:
        """
        查询当前呼叫转移状态
        AT+CCFC=<reason>,2
        """
        resp = self.send_at(f"AT+CCFC={reason},2", timeout=10.0)
        # 匹配: +CCFC: <status>,<class>[,<number>,<type>...]
        # status: 0=未激活, 1=已激活
        match = re.search(r'\+CCFC:\s*(\d+),\s*(\d+)(?:,\s*"([^"]+)",\s*(\d+))?', resp)
        if match:
            status = int(match.group(1))
            number = match.group(3) or ""
            return {
                "success": True,
                "reason": reason,
                "reason_name": self.REASONS.get(reason, "未知"),
                "active": status == 1,
                "number": number,
                "raw": resp,
            }
        
        # 部分网络如果未设置任何转移，会直接返回 +CCFC: 0,... 或 OK
        if "OK" in resp:
            return {
                "success": True,
                "reason": reason,
                "reason_name": self.REASONS.get(reason, "未知"),
                "active": False,
                "number": "",
                "raw": resp,
            }

        return {
            "success": False,
            "error": resp,
            "reason": reason,
            "active": False,
            "number": "",
        }

    def activate(self, reason: int = 0) -> Dict[str, Any]:
        """
        激活已预设的呼叫转移
        AT+CCFC=<reason>,1
        """
        resp = self.send_at(f"AT+CCFC={reason},1", timeout=10.0)
        success = "OK" in resp
        return {"success": success, "action": "activate", "raw": resp}

    def deactivate(self, reason: int = 0) -> Dict[str, Any]:
        """
        停用呼叫转移（保留设置的号码但关闭）
        AT+CCFC=<reason>,0
        """
        resp = self.send_at(f"AT+CCFC={reason},0", timeout=10.0)
        success = "OK" in resp
        return {"success": success, "action": "deactivate", "raw": resp}

    def set_and_activate(self, number: str, reason: int = 0, timeout_sec: Optional[int] = None) -> Dict[str, Any]:
        """
        注册新号码并立即激活
        AT+CCFC=<reason>,3,"<number>",145[,<class>[,<subaddr>[,<satype>[,<time>]]]]
        """
        num_clean = number.strip()
        if reason == 2 and timeout_sec:
            cmd = f'AT+CCFC={reason},3,"{num_clean}",145,1,,,,{timeout_sec}'
        else:
            cmd = f'AT+CCFC={reason},3,"{num_clean}",145'
        resp = self.send_at(cmd, timeout=12.0)
        success = "OK" in resp
        return {"success": success, "action": "set_and_activate", "number": num_clean, "raw": resp}


    def erase(self, reason: int = 0) -> Dict[str, Any]:
        """
        彻底注销清除呼叫转移设置
        AT+CCFC=<reason>,4
        """
        resp = self.send_at(f"AT+CCFC={reason},4", timeout=10.0)
        success = "OK" in resp
        return {"success": success, "action": "erase", "raw": resp}
