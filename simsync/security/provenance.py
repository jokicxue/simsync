"""
SimSync 数字水印与数字版权溯源模块 (Digital Watermarking & Provenance)
包含零宽隐形水印编解码、静态数字指纹与版权取证验证函数。
"""

import hashlib
from typing import Optional

# 作者与项目核心版权指纹
ORIGINAL_AUTHOR = "jokic"
PROJECT_NAME = "SimSync"
SIGNATURE_PHRASE = "SimSync-Original-Author:jokic"
FINGERPRINT_HASH = hashlib.sha256(b"SimSync-Original-Author-jokic-2026").hexdigest()

# 零宽字符常数：\u200b (0), \u200c (1), \ufeff (定界符)
# 字符串 "SimSync-Original-Author:jokic" 的零宽编码序列 (234 字符，肉眼与浏览器完全透明)
ZERO_WIDTH_WATERMARK = (
    "\ufeff\u200b\u200c\u200b\u200c\u200b\u200b\u200c\u200c\u200b\u200c\u200c\u200b\u200c\u200b"
    "\u200b\u200c\u200b\u200c\u200c\u200b\u200c\u200c\u200b\u200c\u200b\u200c\u200b\u200c\u200b"
    "\u200b\u200c\u200c\u200b\u200c\u200c\u200c\u200c\u200b\u200b\u200c\u200b\u200c\u200c\u200b"
    "\u200c\u200c\u200c\u200b\u200b\u200c\u200c\u200b\u200b\u200b\u200c\u200c\u200b\u200b\u200c"
    "\u200b\u200c\u200c\u200b\u200c\u200b\u200c\u200b\u200b\u200c\u200c\u200c\u200c\u200b\u200c"
    "\u200c\u200c\u200b\u200b\u200c\u200b\u200b\u200c\u200c\u200b\u200c\u200b\u200b\u200c\u200b"
    "\u200c\u200c\u200b\u200b\u200c\u200c\u200c\u200b\u200c\u200c\u200b\u200c\u200b\u200b\u200c"
    "\u200b\u200c\u200c\u200b\u200c\u200c\u200c\u200b\u200b\u200c\u200c\u200b\u200b\u200b\u200b"
    "\u200c\u200b\u200c\u200c\u200b\u200c\u200c\u200b\u200b\u200b\u200b\u200c\u200b\u200c\u200c"
    "\u200b\u200c\u200b\u200c\u200b\u200b\u200b\u200b\u200b\u200c\u200b\u200c\u200c\u200c\u200b"
    "\u200c\u200b\u200c\u200b\u200c\u200c\u200c\u200b\u200c\u200b\u200b\u200b\u200c\u200c\u200b"
    "\u200c\u200b\u200b\u200b\u200b\u200c\u200c\u200b\u200c\u200c\u200c\u200c\u200b\u200c\u200c"
    "\u200c\u200b\u200b\u200c\u200b\u200b\u200b\u200c\u200c\u200c\u200b\u200c\u200b\u200b\u200c"
    "\u200c\u200b\u200c\u200b\u200c\u200b\u200b\u200c\u200c\u200b\u200c\u200c\u200c\u200c\u200b"
    "\u200c\u200c\u200b\u200c\u200b\u200c\u200c\u200b\u200c\u200c\u200b\u200c\u200b\u200b\u200c"
    "\u200b\u200c\u200c\u200b\u200b\u200b\u200c\u200c\ufeff"
)


def extract_watermark(text: str) -> Optional[str]:
    """从文本中提取零宽字符隐形水印并解码"""
    if "\ufeff" not in text:
        return None
    try:
        start = text.find("\ufeff")
        end = text.find("\ufeff", start + 1)
        if start == -1 or end == -1:
            return None
        zw_segment = text[start + 1 : end]
        bin_str = "".join("1" if c == "\u200c" else "0" for c in zw_segment if c in ("\u200b", "\u200c"))
        if len(bin_str) % 8 != 0:
            return None
        decoded = "".join(chr(int(bin_str[i : i + 8], 2)) for i in range(0, len(bin_str), 8))
        return decoded
    except Exception:
        return None


def get_provenance_info() -> dict:
    """获取系统原始所有权凭据"""
    return {
        "project": PROJECT_NAME,
        "author": ORIGINAL_AUTHOR,
        "signature_hash": FINGERPRINT_HASH,
        "verified": True,
        "watermark_present": True,
    }
