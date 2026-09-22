import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Optional, Tuple


def generate_totp_secret() -> str:
    """生成符合 RFC 6238 标准的 Base32 TOTP 密钥 (160-bit / 20 bytes)"""
    random_bytes = os.urandom(20)
    return base64.b32encode(random_bytes).decode("utf-8").replace("=", "")


def get_totp_token(secret: str, time_step: int = 30, for_time: Optional[float] = None) -> str:
    """计算指定时刻的 6 位 TOTP 动态口令"""
    if for_time is None:
        for_time = time.time()

    # 规范化 secret (填充 Base32 缺少的 '=')
    secret = secret.strip().replace(" ", "").upper()
    missing_padding = (8 - len(secret) % 8) % 8
    padded_secret = secret + ("=" * missing_padding)

    try:
        key = base64.b32decode(padded_secret, casefold=True)
    except Exception:
        return ""

    counter = int(for_time // time_step)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[19] & 0x0F
    code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def verify_totp(secret: str, token: str, time_step: int = 30, window: int = 1) -> bool:
    """
    验证用户输入的 TOTP 动态口令
    window=1 表示允许前后 30 秒的时钟偏差（容忍度）
    """
    if not secret or not token:
        return False

    token = token.strip()
    if not token.isdigit() or len(token) != 6:
        return False

    current_time = time.time()
    for dt in range(-window, window + 1):
        test_time = current_time + (dt * time_step)
        expected = get_totp_token(secret, time_step, test_time)
        if expected and hmac.compare_digest(expected, token):
            return True

    return False


def generate_otpauth_uri(secret: str, account_name: str = "admin", issuer: str = "SimSync") -> str:
    """
    生成适用于 Google Authenticator / 微软验证器 / 1Password 扫描绑定的 otpauth 链接
    """
    secret = secret.strip().replace(" ", "").upper()
    return f"otpauth://totp/{issuer}:{account_name}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def generate_recovery_codes(count: int = 8) -> Tuple[list[str], list[str]]:
    """
    生成 N 组一次性应急安全码（例如：A8F2-9K3L）
    返回 (明文列表, SHA-256哈希列表)
    """
    import secrets
    # 采用高辨识度字符集（排除 0, O, 1, I 等易混淆字符）
    charset = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    plain_codes = []
    hashed_codes = []

    for _ in range(count):
        p1 = "".join(secrets.choice(charset) for _ in range(4))
        p2 = "".join(secrets.choice(charset) for _ in range(4))
        code = f"{p1}-{p2}"
        plain_codes.append(code)

        # 规范化：去除中划线并转大写，计算 SHA-256
        norm = code.replace("-", "").strip().upper()
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        hashed_codes.append(h)

    return plain_codes, hashed_codes


def verify_and_consume_recovery_code(code: str, hashed_codes: list[str]) -> Tuple[bool, list[str]]:
    """
    校验并核销一组应急安全码
    返回 (是否有效, 核销后的剩余哈希列表)
    """
    if not code or not hashed_codes:
        return False, hashed_codes

    norm = code.replace("-", "").replace(" ", "").strip().upper()
    h = hashlib.sha256(norm.encode("utf-8")).hexdigest()

    if h in hashed_codes:
        updated = [x for x in hashed_codes if x != h]
        return True, updated

    return False, hashed_codes
