import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List


# GSM 03.38 默认 7-bit 字符集映射表
GSM_7BIT_ALPHABET = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# GSM 03.38 扩展字符集 (前缀 0x1B)
GSM_7BIT_EXTENDED = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}


def swap_semi_octets(s: str) -> str:
    """交换半字节，例如 '164132' -> '611423'"""
    res = []
    for i in range(0, len(s), 2):
        if i + 1 < len(s):
            res.append(s[i + 1])
            res.append(s[i])
        else:
            res.append(s[i])
    return "".join(res).rstrip("F").rstrip("f")


def decode_gsm7_bit(data: bytes, length: int, start_septet: int = 0) -> str:
    """
    解码 7-bit 压缩数据
    :param data: 待解码的字节流
    :param length: 目标解码的有效文本 septet 字符数
    :param start_septet: 跳过的前置 septet 数（如 UDH + fill bits 占用的 septet）
    """
    out = []
    bit_buf = 0
    bits_in_buf = 0
    ext = False
    septet_idx = 0

    for b in data:
        bit_buf |= b << bits_in_buf
        bits_in_buf += 8
        while bits_in_buf >= 7:
            char_code = bit_buf & 0x7F
            bit_buf >>= 7
            bits_in_buf -= 7
            septet_idx += 1

            if septet_idx <= start_septet:
                continue

            if ext:
                out.append(GSM_7BIT_EXTENDED.get(char_code, " "))
                ext = False
            elif char_code == 0x1B:
                ext = True
            else:
                if char_code < len(GSM_7BIT_ALPHABET):
                    out.append(GSM_7BIT_ALPHABET[char_code])
                else:
                    out.append("?")

            if (septet_idx - start_septet) >= length:
                break
        if (septet_idx - start_septet) >= length:
            break

    return "".join(out)


class PduDecoder:
    def __init__(self):
        # 缓存长短信分片: {(sender, ref_id, total): {seq: text}}
        self.multipart_cache: Dict[Tuple[str, int, int], Dict[int, str]] = {}
        # 记录分片创建时间用于 TTL 过期清理: {(sender, ref_id, total): timestamp}
        self.multipart_timestamps: Dict[Tuple[str, int, int], float] = {}

    def _clean_expired_multipart(self, ttl_seconds: float = 600.0):
        """清理超过 10 分钟未集齐的孤立分片，防止内存泄露和同 ref_id 冲突"""
        now = time.time()
        expired_keys = [k for k, t in self.multipart_timestamps.items() if now - t > ttl_seconds]
        for k in expired_keys:
            self.multipart_cache.pop(k, None)
            self.multipart_timestamps.pop(k, None)

    def decode(self, pdu_hex: str) -> Optional[Dict[str, Any]]:
        """
        解码 SMS-DELIVER 类型的 PDU 字符串
        返回: {
            'sender': str,
            'timestamp': int (毫秒),
            'readable_date': str,
            'content': str,
            'smsc': str,
            'is_complete': bool
        }
        """
        try:
            self._clean_expired_multipart()
            pdu_hex = pdu_hex.strip()
            if not pdu_hex or len(pdu_hex) < 16:
                return None

            idx = 0

            # 1. SMSC (短信中心) 地址解析
            smsc_len = int(pdu_hex[idx : idx + 2], 16)
            smsc = ""
            has_valid_smsc = False

            if smsc_len == 0:
                has_valid_smsc = True
                idx += 2
            elif len(pdu_hex) >= 2 + smsc_len * 2:
                # 检查接下来的地址类型字节最高位 (bit 7) 是否为 1 (0x80 - 0xFF，如 0x91 国际, 0x81 国内/未知)
                smsc_type = int(pdu_hex[idx + 2 : idx + 4], 16)
                if (smsc_type & 0x80) != 0:
                    has_valid_smsc = True
                    idx += 2  # 跳过长度
                    idx += 2  # 跳过类型
                    smsc_digits_len = (smsc_len - 1) * 2
                    smsc_digits = pdu_hex[idx : idx + smsc_digits_len]
                    idx += smsc_digits_len
                    smsc = swap_semi_octets(smsc_digits)
                    if smsc_type == 0x91 and not smsc.startswith("+"):
                        smsc = "+" + smsc

            if not has_valid_smsc:
                # 若首字节不符合 SMSC 格式，说明模块输出的是不带 SMSC 头的原始 TPDU
                idx = 0
                smsc = ""

            # 2. PDU First Octet (SMS-DELIVER 标志)
            first_octet = int(pdu_hex[idx : idx + 2], 16)
            idx += 2
            mti = first_octet & 0x03  # 00 = SMS-DELIVER
            has_udh = bool(first_octet & 0x40)  # TP-UDHI 是否含有 UDH

            # 3. 发件人号码解析
            sender_digits_len = int(pdu_hex[idx : idx + 2], 16)
            idx += 2
            sender_type = int(pdu_hex[idx : idx + 2], 16)
            idx += 2
            # 字节长度 = (号码长度 + 1) // 2
            sender_bytes_len = (sender_digits_len + 1) // 2
            sender_hex = pdu_hex[idx : idx + sender_bytes_len * 2]
            idx += sender_bytes_len * 2

            sender = ""
            # 3GPP TS 23.040 / 24.011: TON (Type of Number) 为 101 (0x50/0xD0) 时为字母数字编码 (Alphanumeric 7-bit)
            if (sender_type & 0x70) == 0x50:
                sender_raw = bytes.fromhex(sender_hex)
                sender = decode_gsm7_bit(sender_raw, (sender_digits_len * 4) // 7)
            else:
                sender = swap_semi_octets(sender_hex)[:sender_digits_len]
                if (sender_type & 0xF0) == 0x90 and not sender.startswith("+"):
                    sender = "+" + sender

            # 4. TP-PID 协议标识
            idx += 2

            # 5. TP-DCS 数据编码方案 (3GPP TS 23.038)
            tp_dcs = int(pdu_hex[idx : idx + 2], 16)
            idx += 2

            encoding_type = "gsm7"
            coding_group = (tp_dcs >> 4) & 0x0F

            if coding_group in (0x00, 0x01):  # General Data Coding
                alphabet = (tp_dcs >> 2) & 0x03
                if alphabet == 0:
                    encoding_type = "gsm7"
                elif alphabet == 1:
                    encoding_type = "8bit"
                elif alphabet == 2:
                    encoding_type = "ucs2"
                else:
                    encoding_type = "gsm7"
            elif coding_group in (0x02, 0x03):  # Compressed
                alphabet = (tp_dcs >> 2) & 0x03
                encoding_type = "ucs2" if alphabet == 2 else "gsm7"
            elif coding_group == 0x0E:  # 1110: Message Waiting Indication Group (Store UCS2)
                encoding_type = "ucs2"
            elif coding_group == 0x0F:  # 1111: Data coding / message class
                encoding_type = "8bit" if (tp_dcs & 0x04) else "gsm7"
            elif (tp_dcs & 0x0C) == 0x08:
                encoding_type = "ucs2"
            elif (tp_dcs & 0x0C) == 0x04:
                encoding_type = "8bit"
            else:
                encoding_type = "gsm7"

            # 6. TP-SCTS 服务中心时间戳 (7 个八位位组)
            scts_hex = pdu_hex[idx : idx + 14]
            idx += 14
            scts_swapped = swap_semi_octets(scts_hex)
            try:
                year = 2000 + int(scts_swapped[0:2])
                month = int(scts_swapped[2:4])
                day = int(scts_swapped[4:6])
                hour = int(scts_swapped[6:8])
                minute = int(scts_swapped[8:10])
                second = int(scts_swapped[10:12])
                # 时区位 (以 15 分钟为步长)
                tz_byte = int(scts_hex[12:14], 16)
                tz_val = int(swap_semi_octets(scts_hex[12:14]))
                tz_hours = (tz_val & 0x7F) * 15 / 60
                if tz_byte & 0x08:
                    tz_hours = -tz_hours
                dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone(timedelta(hours=tz_hours)))
                timestamp_ms = int(dt.timestamp() * 1000)
                readable_date = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                dt_now = datetime.now()
                timestamp_ms = int(dt_now.timestamp() * 1000)
                readable_date = dt_now.strftime("%Y-%m-%d %H:%M:%S")

            # 7. TP-UDL 用户数据长度
            tp_udl = int(pdu_hex[idx : idx + 2], 16)
            idx += 2

            # 计算预期的 User Data 字节数，杜绝切片溢出或读取末尾杂散字符
            if encoding_type == "gsm7":
                expected_bytes = (tp_udl * 7 + 7) // 8
            else:
                expected_bytes = tp_udl

            ud_hex = pdu_hex[idx : idx + expected_bytes * 2]
            if len(ud_hex) % 2 != 0:
                ud_hex = ud_hex[:-1]
            ud_bytes = bytes.fromhex(ud_hex)

            # 8. 用户数据头 (UDH) 解析 (长短信处理)
            content = ""
            ref_id, total_parts, part_num = 0, 1, 1
            udh_len = 0
            udh_septets = 0

            if has_udh and len(ud_bytes) > 0:
                udh_len = ud_bytes[0]
                total_udh_bytes = 1 + udh_len
                if total_udh_bytes <= len(ud_bytes):
                    udh = ud_bytes[1 : total_udh_bytes]
                else:
                    udh = ud_bytes[1:]

                # GSM 7-bit 模式下计算 UDH 及填充位所消耗的 septet 总数
                # 3GPP TS 23.040 规定：文本 septet 必须对齐到下一个 7-bit 边界
                fill_bits = (7 - ((total_udh_bytes * 8) % 7)) % 7
                udh_septets = (total_udh_bytes * 8 + fill_bits) // 7

                # 检查长短信 IEI (0x00 为 8-bit ref, 0x08 为 16-bit ref)
                u_idx = 0
                while u_idx + 1 < len(udh):
                    iei = udh[u_idx]
                    iedl = udh[u_idx + 1]
                    if u_idx + 2 + iedl > len(udh):
                        break
                    ied = udh[u_idx + 2 : u_idx + 2 + iedl]
                    if iei == 0x00 and iedl == 3:
                        ref_id = ied[0]
                        total_parts = ied[1]
                        part_num = ied[2]
                    elif iei == 0x08 and iedl == 4:
                        ref_id = (ied[0] << 8) | ied[1]
                        total_parts = ied[2]
                        part_num = ied[3]
                    u_idx += 2 + iedl

            # 9. 解码正文
            if encoding_type == "ucs2":
                content_bytes = ud_bytes[1 + udh_len :] if has_udh else ud_bytes
                # 确保偶数字节，截除末尾多余奇数字节防止生成 \ufffd 乱码
                if len(content_bytes) % 2 != 0:
                    content_bytes = content_bytes[: len(content_bytes) - 1]

                # 检测 BOM 标识
                if content_bytes.startswith(b"\xff\xfe"):
                    content = content_bytes.decode("utf-16-le", errors="replace")
                elif content_bytes.startswith(b"\xfe\xff"):
                    content = content_bytes.decode("utf-16-be", errors="replace")
                else:
                    content = content_bytes.decode("utf-16-be", errors="replace")

            elif encoding_type == "8bit":
                content_bytes = ud_bytes[1 + udh_len :] if has_udh else ud_bytes
                # 优先 UTF-8，失败自动回退 GB18030/GBK (国内运营商 8-bit 常见格式)，最后回退 latin1
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        content = content_bytes.decode("gb18030")
                    except UnicodeDecodeError:
                        content = content_bytes.decode("latin1", errors="replace")

            else:
                # 7-bit 编码
                if has_udh:
                    # 使用位流解码器跳过 UDH septet，杜绝传统按字节切片导致的 1-bit 位移乱码
                    text_septets = max(0, tp_udl - udh_septets)
                    content = decode_gsm7_bit(ud_bytes, text_septets, start_septet=udh_septets)
                else:
                    content = decode_gsm7_bit(ud_bytes, tp_udl, start_septet=0)

            # 10. 长短信拼接
            is_complete = True
            if total_parts > 1:
                key = (sender, ref_id, total_parts)
                if key not in self.multipart_cache:
                    self.multipart_cache[key] = {}
                self.multipart_cache[key][part_num] = content
                self.multipart_timestamps[key] = time.time()

                if len(self.multipart_cache[key]) == total_parts:
                    # 收集完整
                    ordered_parts = [self.multipart_cache[key][i] for i in sorted(self.multipart_cache[key].keys())]
                    content = "".join(ordered_parts)
                    self.multipart_cache.pop(key, None)
                    self.multipart_timestamps.pop(key, None)
                    is_complete = True
                else:
                    is_complete = False

            return {
                "sender": sender,
                "timestamp": timestamp_ms,
                "readable_date": readable_date,
                "content": content,
                "smsc": smsc,
                "is_complete": is_complete,
                "raw_pdu": pdu_hex,
            }
        except Exception:
            # PDU 解码失败返回 None
            return None


def parse_text_mode_sms(header: str, body: str) -> Optional[Dict[str, Any]]:
    """
    当模块处于 Text 模式 (+CMGF=1) 时的回退解析器
    支持标准/变体 +CMT 头部提取，以及 UCS2-HEX / GBK-HEX 自动还原解码
    示例 header: +CMT: "+61412345678","","26/09/20,12:00:00+40"
    """
    try:
        sender = ""
        # 兼容带引号与不带引号的发件人号码
        m = re.search(r'\+CMT:\s*(?:"([^"]*)"|([^,]+))', header)
        if m:
            sender = (m.group(1) or m.group(2) or "").strip()

        now = datetime.now()
        timestamp_ms = int(now.timestamp() * 1000)
        readable_date = now.strftime("%Y-%m-%d %H:%M:%S")

        raw_content = body.strip()
        decoded_content = raw_content

        # 智能检测：若 Text 模式下输出的是纯十六进制串（模组 UCS2-HEX 或 GBK-HEX）
        if len(raw_content) >= 4 and re.match(r"^[0-9A-Fa-f]+$", raw_content):
            # 1. 优先尝试 UCS2-HEX (每字符 4 个十六进制位)
            if len(raw_content) % 4 == 0:
                try:
                    c = bytes.fromhex(raw_content).decode("utf-16-be")
                    if c and all(ord(ch) >= 32 or ch in "\r\n\t" for ch in c):
                        decoded_content = c
                except Exception:
                    pass

            # 2. 若 UCS2-HEX 失败且为偶数字长，尝试 GB18030 / GBK 十六进制
            if decoded_content == raw_content and len(raw_content) % 2 == 0:
                try:
                    c = bytes.fromhex(raw_content).decode("gb18030")
                    if c and all(ord(ch) >= 32 or ch in "\r\n\t" for ch in c):
                        decoded_content = c
                except Exception:
                    pass

        return {
            "sender": sender or "未知号码",
            "timestamp": timestamp_ms,
            "readable_date": readable_date,
            "content": decoded_content,
            "smsc": "",
            "is_complete": True,
            "raw_pdu": f"{header}\n{body}",
        }
    except Exception:
        pass
    return None


def encode_sms_submit_pdu(recipient: str, text: str, ref_id: int = 1) -> List[Tuple[int, str]]:
    """
    将目标号码与文本内容编码为 SMS-SUBMIT PDU 列表 (支持 UCS2 中英文、特殊符号及长短信自动分片)
    返回: List[(pdu_length_for_cmgs, full_pdu_hex)]
    """
    # 1. 解析目标号码
    clean_number = recipient.strip()
    is_international = clean_number.startswith("+")
    digits = re.sub(r"\D", "", clean_number)
    addr_len = len(digits)
    addr_type = 0x91 if is_international else 0x81

    # 半字节交换
    padded_digits = digits if len(digits) % 2 == 0 else digits + "F"
    swapped_addr = ""
    for i in range(0, len(padded_digits), 2):
        swapped_addr += padded_digits[i + 1] + padded_digits[i]

    da_hex = f"{addr_len:02X}{addr_type:02X}{swapped_addr}"

    # 2. 判断是否需要长短信分片
    # UCS2 单条最多 70 个字符 (140 字节)；长短信带 UDH (6 字节) 后每条最多 67 个字符 (134 字节)
    chars = list(text)
    total_chars = len(chars)

    if total_chars <= 70:
        # 单条短信
        first_octet = 0x01  # SMS-SUBMIT, no UDH, no VPF
        mr = 0x00
        pid = 0x00
        dcs = 0x08  # UCS2 编码

        ud_bytes = text.encode("utf-16-be")
        udl = len(ud_bytes)
        ud_hex = ud_bytes.hex().upper()

        pdu_without_smsc = f"{first_octet:02X}{mr:02X}{da_hex}{pid:02X}{dcs:02X}{udl:02X}{ud_hex}"
        full_pdu = f"00{pdu_without_smsc}"
        cmgs_len = len(pdu_without_smsc) // 2
        return [(cmgs_len, full_pdu)]
    else:
        # 长短信分片
        part_size = 67
        parts = [chars[i : i + part_size] for i in range(0, total_chars, part_size)]
        total_parts = len(parts)
        result = []

        for seq, part_chars in enumerate(parts, 1):
            first_octet = 0x41  # SMS-SUBMIT + UDHI (含 UDH)
            mr = 0x00
            pid = 0x00
            dcs = 0x08

            # UDH: 05 00 03 <ref_id> <total_parts> <part_num>
            udh = bytes([0x05, 0x00, 0x03, ref_id & 0xFF, total_parts, seq])
            part_text = "".join(part_chars)
            part_bytes = part_text.encode("utf-16-be")

            ud_bytes = udh + part_bytes
            udl = len(ud_bytes)
            ud_hex = ud_bytes.hex().upper()

            pdu_without_smsc = f"{first_octet:02X}{mr:02X}{da_hex}{pid:02X}{dcs:02X}{udl:02X}{ud_hex}"
            full_pdu = f"00{pdu_without_smsc}"
            cmgs_len = len(pdu_without_smsc) // 2
            result.append((cmgs_len, full_pdu))

        return result

