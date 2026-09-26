import glob
import logging
import os
import re
import sys
import threading
import time
from typing import Callable, Optional, Dict, Any, List

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

from simsync.modem.pdu_decoder import PduDecoder, parse_text_mode_sms, encode_sms_submit_pdu
from simsync.modem.call_forward import CallForwardController
from simsync.config import AutoFlightConfig

logger = logging.getLogger("simsync.modem")


def decode_serial_bytes(raw: bytes) -> str:
    """智能解码串口数据，优先 UTF-8，回退 GB18030/GBK，防止中文乱码"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except Exception:
            return raw.decode("utf-8", errors="replace")


def probe_port_at(port_name: str, baudrate: int = 115200) -> Dict[str, Any]:
    """
    探测单个串口是否响应 AT 指令，并验证其是否为具备完整功能的蜂窝主端口
    """
    item = {
        "port": port_name,
        "is_at": False,
        "is_modem": False,
        "status_text": "无 AT 响应",
        "description": "未检测到 AT 响应",
        "raw_resp": "",
    }
    try:
        s = serial.Serial(port=port_name, baudrate=baudrate, timeout=0.8, write_timeout=0.8)
        s.reset_input_buffer()

        def _read_with_deadline(timeout_sec: float = 0.8) -> str:
            start_t = time.time()
            buf = bytearray()
            while time.time() - start_t < timeout_sec:
                if s.in_waiting:
                    chunk = s.read(s.in_waiting)
                    buf.extend(chunk)
                    if b"OK" in buf or b"ERROR" in buf:
                        break
                time.sleep(0.04)
            return decode_serial_bytes(bytes(buf)).strip()

        # 1. 发送初次 AT 握手
        s.write(b"AT\r\n")
        s.flush()
        resp = _read_with_deadline(0.5)

        # 若未收到响应，可能模组刚通电或处于省电休眠，发送二次唤醒 AT
        if not resp or "OK" not in resp:
            s.write(b"AT\r\n")
            s.flush()
            resp = _read_with_deadline(0.6)

        item["raw_resp"] = resp

        if "OK" in resp:
            item["is_at"] = True
            item["status_text"] = "有 AT 响应 (OK)"

            # 2. 进一步探测芯片型号 (ATI)
            s.write(b"ATI\r\n")
            s.flush()
            ati_resp = _read_with_deadline(0.5)
            desc_match = re.search(r"(Air\d{3}[A-Z]*|EC\d{3}[A-Z]*|SIM\d{4}[A-Z]*|ME909[A-Z]*|[A-Za-z0-9_-]{4,})", ati_resp)
            if desc_match:
                item["description"] = f"{desc_match.group(0)} (响应正常 OK)"
            else:
                item["description"] = "响应正常 (OK)"

            # 3. 验证是否为主力蜂窝管理端口 (测试 AT+CPIN? 识别 SIM 交互能力)
            s.write(b"AT+CPIN?\r\n")
            s.flush()
            cpin_resp = _read_with_deadline(0.6)
            if "+CPIN:" in cpin_resp or "SIM" in cpin_resp:
                item["is_modem"] = True
                item["description"] += " [主力蜂窝端口]"

            # 标注物理设备软链接对应名称 (如 ttyACM0)
            try:
                if os.path.islink(port_name):
                    real_dev = os.path.basename(os.path.realpath(port_name))
                    item["description"] += f" ({real_dev})"
            except Exception:
                pass

        else:
            item["status_text"] = "无 AT 响应"
            item["description"] = "未返回 OK"
            try:
                if os.path.islink(port_name):
                    real_dev = os.path.basename(os.path.realpath(port_name))
                    item["description"] += f" ({real_dev})"
            except Exception:
                pass

        s.close()
    except Exception as e:
        item["status_text"] = "无法打开"
        item["description"] = f"打开失败: {e}"

    return item


def scan_available_ports() -> List[Dict[str, Any]]:
    """
    扫描系统中所有可用串口，并逐个发送 AT 探测，返回探测详情
    """
    candidate_ports: List[str] = []

    # 1. 搜集候选串口
    if sys.platform.startswith("win"):
        if serial and hasattr(serial.tools, "list_ports"):
            candidate_ports = [p.device for p in serial.tools.list_ports.comports()]
        else:
            candidate_ports = [f"COM{i}" for i in range(1, 33)]
    else:
        # Linux / Docker 宿主机 / OpenWrt
        # 收集所有设备并解析真实设备路径 (realpath) 进行去重，避免同一个设备因为 by-id / by-path / ttyACM 重复探测 3 次
        real_to_best_alias: Dict[str, str] = {}
        all_found = []
        for pattern in ["/dev/serial/by-id/*", "/dev/serial/by-path/*", "/dev/ttyACM*", "/dev/ttyUSB*", "/dev/ttyAMA*"]:
            for p in sorted(glob.glob(pattern)):
                all_found.append(p)
        if serial and hasattr(serial.tools, "list_ports"):
            for p in serial.tools.list_ports.comports():
                all_found.append(p.device)

        # 优先保留 by-id 路径（插拔换口不漂移），其次保留原生设备名
        for p in all_found:
            try:
                real = os.path.realpath(p)
            except Exception:
                real = p
            if real not in real_to_best_alias:
                real_to_best_alias[real] = p
            else:
                # 如果当前是 by-id 路径，优先使用它（最稳定）
                if "/by-id/" in p:
                    real_to_best_alias[real] = p

        candidate_ports = sorted(list(real_to_best_alias.values()))

    results: List[Dict[str, Any]] = []

    if serial is None:
        logger.error("pyserial 库未安装，无法扫描串口")
        return results

    for port_name in candidate_ports:
        results.append(probe_port_at(port_name))

    return results


class ModemClient:
    def __init__(
        self,
        port: str = "auto",
        baudrate: int = 115200,
        timeout: float = 5.0,
        auto_hangup: bool = True,
        auto_flight: Optional[AutoFlightConfig] = None,
        on_sms_received: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_call_received: Optional[Callable[[str, str], None]] = None,
        on_flight_changed: Optional[Callable[[str, str], None]] = None,
        phone_number: Optional[str] = "",
    ):
        self.configured_port = port  # 保存用户配置策略 ("auto" 或指定设备路径)
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.auto_hangup = auto_hangup
        self.auto_flight = auto_flight or AutoFlightConfig()
        self.on_sms_received = on_sms_received
        self.on_call_received = on_call_received
        self.on_flight_changed = on_flight_changed

        self.ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None

        self.pdu_decoder = PduDecoder()
        self.call_forward = CallForwardController(self.send_at)

        self.is_connected = False
        self.is_flight_mode = False

        # 串口缓存 (开机探测一次，不点击重新扫描不再次探测)
        self.cached_ports: List[Dict[str, Any]] = []

        # 模组硬件型号与固件版本
        self.model = ""
        self.revision = ""

        # 信号与网络参数
        self.signal_quality = "Unknown"
        self.csq = "--"
        self.rsrp = "--"
        self.rsrq = "--"
        self.sinr = "--"
        self.operator_name = "Searching..."
        self.roaming_status = "Unknown"
        self.imei = ""
        self.iccid = ""
        self.phone_number = (phone_number or "").strip()

        # 来电号码临时暂存
        self._last_caller = "未知来电"

        # 自动飞行模式定时器
        self._auto_flight_timer: Optional[threading.Timer] = None
        self._auto_flight_deadline: Optional[float] = None
        self._auto_flight_reason: str = ""

    def start(self):
        self._running = True
        self._reader_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._running = False
        self._cancel_auto_flight()
        self._close_serial()

    def scan_ports_now(self) -> List[Dict[str, Any]]:
        """主动执行一次串口扫描并更新缓存"""
        self.cached_ports = scan_available_ports()
        return self.cached_ports

    def _open_serial(self) -> bool:
        if serial is None:
            logger.error("pyserial 库未安装！")
            return False

        # 如果没有缓存过串口列表，开机扫描探测一次
        if not self.cached_ports:
            self.cached_ports = scan_available_ports()

        target_port = self.configured_port or self.port or "auto"

        # 若配置为 auto，自动寻找最佳 AT 模组端口
        if target_port == "auto":
            logger.info("串口配置策略为 'auto'，正在扫描并选择最佳 AT 模组设备...")
            at_ports = [p for p in self.cached_ports if p["is_at"]]
            # 优先选择具备完整蜂窝能力的端口 (is_modem=True)，避免误选中多端口模组的调试/GPS日志口
            modem_ports = [p for p in at_ports if p.get("is_modem")]
            preferred_ports = modem_ports or at_ports

            if preferred_ports:
                target_port = preferred_ports[0]["port"]
                logger.info(f"自动绑定最佳 AT 串口: {target_port} ({preferred_ports[0]['description']})")
                self.port = target_port
            else:
                # 若缓存中没找到，重新主动探测一次
                self.cached_ports = scan_available_ports()
                at_ports = [p for p in self.cached_ports if p["is_at"]]
                modem_ports = [p for p in at_ports if p.get("is_modem")]
                preferred_ports = modem_ports or at_ports
                if preferred_ports:
                    target_port = preferred_ports[0]["port"]
                    logger.info(f"重新探测自动绑定最佳 AT 串口: {target_port} ({preferred_ports[0]['description']})")
                    self.port = target_port
                else:
                    logger.warning("未检测到任何响应 AT 的串口设备，将在 5 秒后重试...")
                    return False
        else:
            # 用户指定了固定的设备路径 (例如 /dev/serial/by-id/... 或 COM3)
            # 在 Linux 下如果设备拔出，节点不存在，则等待设备插入，不篡改用户的配置
            if not sys.platform.startswith("win") and not os.path.exists(target_port):
                logger.warning(f"配置指定的串口设备不存在或未连接: {target_port}，等待设备就绪 (5 秒后重试)...")
                return False
            self.port = target_port

        try:
            self.ser = serial.Serial(
                port=target_port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self.is_connected = True
            logger.info(f"成功打开串口: {target_port} (波特率: {self.baudrate})")
            return True
        except Exception as e:
            self.is_connected = False
            logger.warning(f"打开串口 {target_port} 失败: {e}，将在 5 秒后重试...")
            return False

    def _close_serial(self):
        self.is_connected = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def switch_port(self, new_port: str) -> bool:
        """在线切换串口设备"""
        logger.info(f"收到切换串口请求: {self.port} -> {new_port}")
        with self._lock:
            self._close_serial()
            self.configured_port = new_port
            self.port = new_port
            if new_port == "auto":
                self.cached_ports = []
            if self._open_serial():
                self._init_modem()
                return True
            return False

    def send_at(self, cmd: str, timeout: float = 3.0) -> str:
        """线程安全发送 AT 指令并等待响应"""
        if not self.ser or not self.ser.is_open:
            return "ERROR: Serial not open"

        with self._lock:
            try:
                self.ser.reset_input_buffer()
                full_cmd = (cmd.strip() + "\r\n").encode("utf-8")
                self.ser.write(full_cmd)
                self.ser.flush()

                start_time = time.time()
                lines = []
                while time.time() - start_time < timeout:
                    if self.ser.in_waiting:
                        line = decode_serial_bytes(self.ser.readline()).strip()
                        if line:
                            lines.append(line)
                            if line in ["OK", "ERROR"] or "CME ERROR" in line or "CMS ERROR" in line:
                                break
                    else:
                        time.sleep(0.05)

                return "\n".join(lines)
            except Exception as e:
                logger.error(f"发送 AT 指令 [{cmd}] 异常: {e}")
                return f"ERROR: {e}"

    def execute_raw_at(self, cmd: str, timeout: float = 5.0) -> str:
        """执行任意原始 AT 指令（供 Web 终端使用）"""
        return self.send_at(cmd, timeout=timeout)

    def _detect_model_and_revision(self):
        """自动探测模组型号与固件版本"""
        if not self.model:
            # 1. 优先尝试标准 3GPP 指令 AT+CGMM
            cgmm_resp = self.send_at("AT+CGMM")
            lines = [l.strip() for l in cgmm_resp.splitlines() if l.strip() and l.strip() not in ["OK", "ERROR"] and "AT+CGMM" not in l and not l.startswith("AT")]
            if lines:
                clean_model = lines[0].replace("+CGMM:", "").strip()
                if clean_model:
                    self.model = clean_model
            
            # 2. 若无结果，尝试通用 ATI 指令
            if not self.model:
                ati_resp = self.send_at("ATI")
                lines = [l.strip() for l in ati_resp.splitlines() if l.strip() and l.strip() not in ["OK", "ERROR"] and "ATI" not in l and not l.startswith("AT")]
                if lines:
                    for l in lines:
                        m = re.search(r"\b(Air\d{3}[A-Za-z0-9_-]*|EC\d{3}[A-Za-z0-9_-]*|EG\d{3}[A-Za-z0-9_-]*|SIM\d{4}[A-Za-z0-9_-]*|ME909[A-Za-z0-9_-]*|[A-Za-z0-9_-]{4,})\b", l)
                        if m:
                            self.model = m.group(1)
                            break
                    if not self.model and lines:
                        self.model = lines[0]

        if not self.revision:
            cgmr_resp = self.send_at("AT+CGMR")
            lines = [l.strip() for l in cgmr_resp.splitlines() if l.strip() and l.strip() not in ["OK", "ERROR"] and "AT+CGMR" not in l and not l.startswith("AT")]
            if lines:
                self.revision = lines[0].replace("+CGMR:", "").strip()
            else:
                gmr_resp = self.send_at("AT+GMR")
                lines = [l.strip() for l in gmr_resp.splitlines() if l.strip() and l.strip() not in ["OK", "ERROR"] and "AT+GMR" not in l and not l.startswith("AT")]
                if lines:
                    self.revision = lines[0].replace("+GMR:", "").strip()

    def _init_modem(self):
        """初始化模块，严格遵守 0 流量规范"""
        # 1. 探测型号与固件版本
        self._detect_model_and_revision()
        logger.info(f"开始执行模组初始化序列 ({self.model or '4G 蜂窝模组'})...")
        time.sleep(1)

        # 1. 关闭回显
        self.send_at("ATE0")
        # 2. 开启详细错误提示
        self.send_at("AT+CMEE=2")
        # 3. 开启来电显示提示
        self.send_at("AT+CLIP=1")
        # 4. 设置标准字符集为 GSM
        self.send_at('AT+CSCS="GSM"')
        # 5. 设置短信为 PDU 模式 (支持国际编码与长短信)
        self.send_at("AT+CMGF=0")
        # 6. 设置新短信直接串口上报 (+CMT)，不保存在 SIM 卡，防存满且秒级通知；若不支持则回退 2,1
        cnmi_resp = self.send_at("AT+CNMI=2,2,0,0,0")
        if "ERROR" in cnmi_resp:
            logger.warning("模组不支持 AT+CNMI=2,2,0,0,0，尝试回退模式 AT+CNMI=2,1,0,0,0...")
            self.send_at("AT+CNMI=2,1,0,0,0")

        # 读取设备状态
        self.refresh_status()
        logger.info(
            f"模组初始化就绪 [{self.model or '4G 蜂窝模组'}] | 运营商: {self.operator_name} | 信号: {self.signal_quality} | RSRP: {self.rsrp} | RSRQ: {self.rsrq}"
        )

    def refresh_status(self):
        """刷新信号强度(CSQ/RSRP/RSRQ)、运营商、漫游状态及飞行模式"""
        # 模组型号与固件探测
        if not self.model or not self.revision:
            if not self.is_flight_mode:
                self._detect_model_and_revision()

        # 飞行模式查询
        cfun_resp = self.send_at("AT+CFUN?")
        m_cfun = re.search(r"\+CFUN:\s*(\d+)", cfun_resp)
        if m_cfun:
            cfun_val = int(m_cfun.group(1))
            self.is_flight_mode = (cfun_val == 4)
        else:
            self.is_flight_mode = False

        if self.is_flight_mode:
            self.signal_quality = "飞行模式已开启 (射频休眠)"
            self.csq = "--"
            self.rsrp = "--"
            self.rsrq = "--"
            self.operator_name = "飞行模式"
            self.roaming_status = "飞行模式 (未发射信号)"
        else:
            # 1. 信号 CSQ
            csq_resp = self.send_at("AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),(\d+)", csq_resp)
            if m:
                rssi = int(m.group(1))
                if rssi == 99:
                    self.csq = "无信号 (99)"
                    self.signal_quality = "无信号 (99)"
                else:
                    pct = min(100, int(rssi * 100 / 31))
                    self.csq = f"{rssi} ({pct}%)"
                    self.signal_quality = f"{rssi} (约 {pct}% 强度)"

            # 2. 扩展信号质量 AT+CESQ (RSRP / RSRQ)
            cesq_resp = self.send_at("AT+CESQ")
            m_cesq = re.search(r"\+CESQ:\s*(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)", cesq_resp)
            if m_cesq:
                rsrq_raw = int(m_cesq.group(5))
                rsrp_raw = int(m_cesq.group(6))
                if rsrp_raw != 255:
                    self.rsrp = f"{rsrp_raw - 140} dBm"
                else:
                    self.rsrp = "--"
                if rsrq_raw != 255:
                    self.rsrq = f"{round(rsrq_raw * 0.5 - 19.5, 1)} dB"
                else:
                    self.rsrq = "--"
            else:
                # 尝试 EC618 专有状态指令
                ec_resp = self.send_at("AT+ECSTATUS?")
                m_rsrp = re.search(r'RSRP[:\s]*(-?\d+)', ec_resp, re.IGNORECASE)
                m_rsrq = re.search(r'RSRQ[:\s]*(-?\d+)', ec_resp, re.IGNORECASE)
                if m_rsrp:
                    self.rsrp = f"{m_rsrp.group(1)} dBm"
                if m_rsrq:
                    self.rsrq = f"{m_rsrq.group(1)} dB"

            # 运营商 COPS
            cops_resp = self.send_at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', cops_resp)
            if m:
                self.operator_name = m.group(1)
            else:
                self.operator_name = "未注册 / 搜网中"

            # 漫游状态 CREG
            creg_resp = self.send_at("AT+CREG?")
            m = re.search(r"\+CREG:\s*\d+,(\d+)", creg_resp)
            if m:
                stat = int(m.group(1))
                status_map = {
                    0: "未注册，未搜网",
                    1: "已注册 (本地网络)",
                    2: "未注册，正在搜网",
                    3: "注册被拒绝",
                    4: "未知状态",
                    5: "已注册 (漫游状态 - 正常)",
                }
                self.roaming_status = status_map.get(stat, f"状态码 {stat}")


        # IMEI
        if not self.imei:
            imei_resp = self.send_at("AT+CGSN")
            m = re.search(r"\b\d{15}\b", imei_resp)
            if m:
                self.imei = m.group(0)

        # ICCID
        if not self.iccid:
            iccid_resp = self.send_at("AT+CCID")
            m = re.search(r"\b[0-9A-Fa-f]{19,20}\b", iccid_resp)
            if m:
                self.iccid = m.group(0)

        # 本机手机号 (MSISDN) 查询 (AT+CNUM)
        if not self.phone_number and not self.is_flight_mode:
            cnum_resp = self.send_at("AT+CNUM")
            # 格式: +CNUM: "","+8613800000000",145 或 +CNUM: "My Number","13800000000",129
            m_num = re.search(r'\+CNUM:\s*(?:"[^"]*")?\s*,\s*"([^"]+)"', cnum_resp)
            if m_num:
                self.phone_number = m_num.group(1).strip()

    def set_phone_number(self, new_number: str) -> bool:
        """设置本机手机号码并尝试写入 SIM 卡 EF_MSISDN"""
        self.phone_number = new_number.strip()
        if self.ser and self.ser.is_open:
            try:
                self.send_at('AT+CPBS="ON"')
                self.send_at(f'AT+CPBW=1,"{self.phone_number}",129,"SIM"')
            except Exception as e:
                logger.debug(f"尝试向 SIM 卡写入本机号码忽略: {e}")
        return True

    # ==================== 飞行模式与电源管理 ====================

    def set_flight_mode(self, enable: bool, reason: str = "") -> bool:
        """设置飞行模式 (enable=True 开启飞行模式 AT+CFUN=4；False 恢复正常 AT+CFUN=1)"""
        cmd = "AT+CFUN=4" if enable else "AT+CFUN=1"
        action_name = "开启飞行模式(休眠射频)" if enable else "恢复正常模式(搜网)"
        logger.info(f"正在切换飞行模式: {action_name} [{cmd}] 原因: {reason or '手动/策略'}")
        resp = self.send_at(cmd, timeout=8.0)
        if "OK" in resp:
            self.is_flight_mode = enable
            if enable:
                self._cancel_auto_flight()
            else:
                # 退出飞行模式后，模组射频重新初始化，确保 PDU 模式与 URC 接收配置重新生效
                time.sleep(0.5)
                self.send_at("ATE0")
                self.send_at('AT+CSCS="GSM"')
                self.send_at("AT+CMGF=0")
                cnmi_resp = self.send_at("AT+CNMI=2,2,0,0,0")
                if "ERROR" in cnmi_resp:
                    self.send_at("AT+CNMI=2,1,0,0,0")
            self.refresh_status()
            if self.on_flight_changed:
                try:
                    self.on_flight_changed("enter_flight" if enable else "wake_up", reason or ("手动开启飞行" if enable else "手动唤醒"))
                except Exception as e:
                    logger.error(f"执行 on_flight_changed 回调异常: {e}")
            return True
        logger.error(f"切换飞行模式失败: {resp}")
        return False

    def reboot_modem(self) -> bool:
        """软重启模块 (AT+CFUN=1,1)"""
        logger.warning("正在向模块发送软重启指令 AT+CFUN=1,1...")
        resp = self.send_at("AT+CFUN=1,1", timeout=5.0)
        time.sleep(2)
        return "OK" in resp

    def wait_for_network(self, timeout: float = 30.0) -> bool:
        """从飞行模式唤醒后等待网络驻留成功"""
        start = time.time()
        logger.info("正在等待模块网络驻留完成...")
        while time.time() - start < timeout:
            resp = self.send_at("AT+CREG?", timeout=2.0)
            m = re.search(r"\+CREG:\s*\d+,(\d+)", resp)
            if m:
                stat = int(m.group(1))
                if stat in (1, 5):
                    logger.info(f"网络驻留成功 (CREG={stat})，耗时 {round(time.time() - start, 1)}s")
                    return True
            time.sleep(1.5)
        logger.warning(f"等待网络驻留超时 ({timeout}s)")
        return False

    def _schedule_auto_flight(self, delay_minutes: int, reason: str = ""):
        """设置延迟进入飞行模式的定时器"""
        self._cancel_auto_flight()
        delay_sec = max(5, delay_minutes * 60)
        self._auto_flight_deadline = time.time() + delay_sec
        self._auto_flight_reason = reason
        logger.info(f"已设定自动飞行倒计时: {delay_minutes} 分钟后执行 ({reason})")

        def _action():
            logger.info(f"自动飞行倒计时结束，正在进入飞行模式 ({self._auto_flight_reason})...")
            self.set_flight_mode(True, self._auto_flight_reason or "自动延时休眠")

        self._auto_flight_timer = threading.Timer(delay_sec, _action)
        self._auto_flight_timer.daemon = True
        self._auto_flight_timer.start()

    def _cancel_auto_flight(self):
        """取消当前自动飞行定时器"""
        if self._auto_flight_timer:
            try:
                self._auto_flight_timer.cancel()
            except Exception:
                pass
            self._auto_flight_timer = None
        self._auto_flight_deadline = None
        self._auto_flight_reason = ""

    def get_auto_flight_status(self) -> Dict[str, Any]:
        """获取自动飞行模式及倒计时状态"""
        now = time.time()
        remaining = 0
        if self._auto_flight_deadline and self._auto_flight_deadline > now:
            remaining = int(self._auto_flight_deadline - now)
        return {
            "is_flight_mode": self.is_flight_mode,
            "auto_flight_enabled": self.auto_flight.enabled,
            "timer_active": bool(self._auto_flight_timer and remaining > 0),
            "remaining_seconds": remaining,
            "reason": self._auto_flight_reason,
        }

    # ==================== 短信发送 ====================

    def send_sms(self, recipient: str, content: str) -> Dict[str, Any]:
        """
        向指定号码发送短信（支持中英文、长短信分片、自动唤醒与自动休眠）
        返回: {"success": bool, "error": str}
        """
        if not recipient or not content:
            return {"success": False, "error": "号码或内容不能为空"}

        # 如果处于飞行模式，判断是否允许自动唤醒
        if self.is_flight_mode:
            if self.auto_flight.wake_before_send:
                logger.info("模块当前处于飞行模式，正在临时唤醒以发送短信...")
                if not self.set_flight_mode(False):
                    return {"success": False, "error": "唤醒射频失败"}
                if not self.wait_for_network(30.0):
                    return {"success": False, "error": "唤醒后等待网络驻留超时"}
            else:
                return {"success": False, "error": "当前处于飞行模式且未开启自动唤醒"}

        # 将短信编码为 PDU
        try:
            pdu_list = encode_sms_submit_pdu(recipient, content)
        except Exception as e:
            return {"success": False, "error": f"PDU 编码失败: {e}"}

        logger.info(f"开始发送短信至 {recipient} (共 {len(pdu_list)} 个分片)...")

        with self._lock:
            try:
                # 确保进入 PDU 模式
                self.ser.reset_input_buffer()
                self.ser.write(b"AT+CMGF=0\r\n")
                self.ser.flush()
                time.sleep(0.1)

                for idx, (cmgs_len, pdu_hex) in enumerate(pdu_list, 1):
                    # 1. 发送 AT+CMGS=<length>\r
                    cmd = f"AT+CMGS={cmgs_len}\r".encode("utf-8")
                    self.ser.reset_input_buffer()
                    self.ser.write(cmd)
                    self.ser.flush()

                    # 2. 等待 '> ' 提示符
                    prompt_found = False
                    start = time.time()
                    while time.time() - start < 4.0:
                        if self.ser.in_waiting:
                            chunk = self.ser.read(self.ser.in_waiting)
                            if b">" in chunk:
                                prompt_found = True
                                break
                        time.sleep(0.05)

                    if not prompt_found:
                        return {"success": False, "error": f"等待 CMGS 提示符超时 (分片 {idx}/{len(pdu_list)})"}

                    # 3. 发送 PDU 十六进制字符串并以 Ctrl+Z (0x1A) 结尾
                    self.ser.write(f"{pdu_hex}\x1a".encode("utf-8"))
                    self.ser.flush()

                    # 4. 等待发送结果 (最长 20 秒)
                    send_success = False
                    err_msg = ""
                    start_wait = time.time()
                    while time.time() - start_wait < 20.0:
                        if self.ser.in_waiting:
                            resp_line = self.ser.readline().decode("utf-8", errors="replace").strip()
                            if resp_line:
                                if "OK" in resp_line or "+CMGS:" in resp_line:
                                    send_success = True
                                    break
                                elif "ERROR" in resp_line:
                                    err_msg = resp_line
                                    break
                        time.sleep(0.1)

                    if not send_success:
                        return {"success": False, "error": f"分片 {idx} 发送失败: {err_msg or '响应超时'}"}

                    logger.info(f"分片 {idx}/{len(pdu_list)} 发送成功")
                    time.sleep(0.5)

            except Exception as e:
                logger.error(f"发送短信异常: {e}")
                return {"success": False, "error": str(e)}

        logger.info(f"短信成功发送至 {recipient}: {content[:30]}...")

        # 发送成功后检查是否需要自动进入飞行模式
        if self.auto_flight.enabled and self.auto_flight.auto_flight_on_sent:
            self._schedule_auto_flight(self.auto_flight.delay_minutes_on_sent, "短信发送后自动休眠")

        return {"success": True}

    # ==================== 后台串口监听循环 ====================

    def _worker_loop(self):
        """后台串口监听主循环"""
        while self._running:
            if not self.ser or not self.ser.is_open:
                if self._open_serial():
                    self._init_modem()
                else:
                    time.sleep(5)
                    continue

            try:
                line = ""
                # 如果没有加锁，说明当前没有同步 AT 命令在执行，我们可以读取主动上报 URC
                if not self._lock.locked() and self.ser.in_waiting:
                    raw_line = self.ser.readline()
                    line = decode_serial_bytes(raw_line).strip()
                else:
                    time.sleep(0.05)
                    continue

                if not line:
                    continue

                # ================= 来电处理 =================
                if line == "RING":
                    logger.warning(f"检测到来电振铃！当前记录号码: {self._last_caller}")
                    # 如果当前号码还是未知，稍等最长 0.4 秒抓取伴随出现的 +CLIP
                    if self._last_caller == "未知来电":
                        clip_start = time.time()
                        while time.time() - clip_start < 0.4:
                            if self.ser and self.ser.in_waiting:
                                sub_line = decode_serial_bytes(self.ser.readline()).strip()
                                if sub_line.startswith("+CLIP:"):
                                    m_clip = re.search(r'\+CLIP:\s*"([^"]+)"', sub_line)
                                    if m_clip:
                                        self._last_caller = m_clip.group(1)
                                        logger.info(f"成功伴随抓取到来电号码: {self._last_caller}")
                                        break
                            time.sleep(0.05)

                    if self.auto_hangup:
                        # 0.5 秒内极速挂断，杜绝漫游扣费
                        hangup_resp = self.send_at("ATH", timeout=1.0)
                        logger.info(f"已自动发送 ATH 挂断来电: {hangup_resp} | 来电号码: {self._last_caller}")
                        if self.on_call_received:
                            self.on_call_received(self._last_caller, "hangup")
                        self._last_caller = "未知来电"

                elif line.startswith("+CLIP:"):
                    # 格式: +CLIP: "+61412345678",145,"",0,"",0
                    m = re.search(r'\+CLIP:\s*"([^"]+)"', line)
                    if m:
                        self._last_caller = m.group(1)
                        logger.info(f"检测到来电号码: {self._last_caller}")

                # ================= 短信处理 (+CMT 串口直报) =================
                elif line.startswith("+CMT:"):
                    # 最多等待 1.5 秒抓取伴随的短信正文/PDU行，跳过空行
                    pdu_or_content = ""
                    t_start = time.time()
                    while time.time() - t_start < 1.5:
                        if self.ser and self.ser.in_waiting:
                            raw_chunk = self.ser.readline()
                            sub_line = decode_serial_bytes(raw_chunk).strip()
                            if sub_line:
                                pdu_or_content = sub_line
                                break
                        time.sleep(0.02)

                    logger.info(f"检测到新短信上报: {line} | 数据: {pdu_or_content}")

                    sms_data = None
                    if pdu_or_content and re.match(r"^[0-9A-Fa-f]{16,}$", pdu_or_content):
                        sms_data = self.pdu_decoder.decode(pdu_or_content)

                    if not sms_data and pdu_or_content:
                        sms_data = parse_text_mode_sms(line, pdu_or_content)

                    if sms_data:
                        if sms_data.get("is_complete"):
                            logger.info(f"收到完整短信: [{sms_data['sender']}] {sms_data['content']}")
                            if self.on_sms_received:
                                self.on_sms_received(sms_data)

                            # 收到短信后，若配置了自动飞行模式，则重置/启动休眠倒计时
                            if self.auto_flight.enabled and self.auto_flight.auto_flight_on_received:
                                self._schedule_auto_flight(
                                    self.auto_flight.delay_minutes_on_received,
                                    "短信接收后自动休眠",
                                )
                        else:
                            logger.info("收到长短信分片，等待其余分片拼接...")

                # ================= 短信处理 (+CMTI SIM/ME 存储提醒) =================
                elif line.startswith("+CMTI:"):
                    logger.info(f"检测到 SIM/ME 新短信存储通知: {line}")
                    m_cmti = re.search(r'\+CMTI:\s*"([A-Za-z]+)"\s*,\s*(\d+)', line)
                    if m_cmti:
                        mem = m_cmti.group(1)
                        idx_num = m_cmti.group(2)
                        self._handle_stored_sms(mem, idx_num)

            except Exception as e:
                logger.error(f"串口监听异常: {e}")
                self._close_serial()
                time.sleep(3)

    def _handle_stored_sms(self, mem: str, index: str):
        """读取 SIM/ME 存储的短信，解码后自动删除释放卡槽，防止卡满"""
        try:
            # 确保处于 PDU 模式读取
            self.send_at("AT+CMGF=0")
            read_resp = self.send_at(f"AT+CMGR={index}")
            # +CMGR: <stat>,[<alpha>],<length>\r\n<pdu>
            pdu_hex = ""
            for l in read_resp.splitlines():
                l = l.strip()
                if l and not l.startswith("+CMGR") and not l.startswith("AT") and l not in ("OK", "ERROR"):
                    if re.match(r"^[0-9A-Fa-f]{16,}$", l):
                        pdu_hex = l
                        break

            if pdu_hex:
                sms_data = self.pdu_decoder.decode(pdu_hex)
                if sms_data and sms_data.get("is_complete"):
                    logger.info(f"成功读取并解码存储短信: [{sms_data['sender']}] {sms_data['content']}")
                    if self.on_sms_received:
                        self.on_sms_received(sms_data)

            # 读取后立即删除该卡槽，防止卡满
            self.send_at(f"AT+CMGD={index}")
            logger.info(f"已清理卡槽短信: {mem} 索引 {index}")
        except Exception as e:
            logger.error(f"处理存储短信卡槽 {mem}:{index} 异常: {e}")
