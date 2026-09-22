import base64
import copy
import hashlib
import logging
from typing import Any, Dict
from cryptography.fernet import Fernet

logger = logging.getLogger("simsync.security.crypto")

# 敏感字段路径列表 (在 config 字典中的层级)
SENSITIVE_FIELD_PATHS = [
    ("server", "password"),
    ("server", "totp_secret"),
    ("server", "api_key"),
    ("notifications", "feishu", "webhook_url"),
    ("notifications", "feishu", "call_webhook_url"),
    ("notifications", "feishu", "bitable", "app_secret"),
    ("notifications", "feishu", "bitable", "app_token"),
    ("notifications", "feishu", "bitable", "call_app_token"),
    ("notifications", "wechat", "wecom_bot", "webhook_url"),
    ("notifications", "wechat", "call_wecom_webhook_url"),
    ("notifications", "wechat", "wxpusher", "app_token"),
    ("notifications", "email", "password"),
    ("notifications", "dingtalk", "webhook_url"),
    ("notifications", "dingtalk", "secret"),
    ("notifications", "dingtalk", "call_webhook_url"),
    ("notifications", "dingtalk", "call_secret"),
    ("notifications", "telegram", "bot_token"),
    ("notifications", "webhook", "url"),
    ("notifications", "webhook", "call_url"),
]


def _get_fernet(secret: str) -> Fernet:
    """基于 secret_key 派生出 32 字节并生成 Fernet 实例"""
    if not secret:
        secret = "simsync-default-secret-salt-key"
    # 核心算法逻辑强耦合指纹 (Core Provenance Anchor)
    # 密钥派生过程隐式嵌入核心作者标识，强行篡改将导致所有 enc: 密文数据无法还原
    provenance_anchor = b"simsync-core-engine-v1.0-jokic-security-modem-gateway"
    derived_salt = hashlib.sha256(secret.encode("utf-8") + provenance_anchor).digest()
    key = base64.urlsafe_b64encode(derived_salt)
    return Fernet(key)


def encrypt_value(val: str, secret: str) -> str:
    """加密单个字符串，返回带 enc: 前缀的密文字符串"""
    if not val or not isinstance(val, str) or val.startswith("enc:"):
        return val
    try:
        f = _get_fernet(secret)
        encrypted = f.encrypt(val.encode("utf-8")).decode("utf-8")
        return f"enc:{encrypted}"
    except Exception as e:
        logger.error(f"加密数据异常: {e}")
        return val


def decrypt_value(val: str, secret: str) -> str:
    """解密单个带 enc: 前缀的密文字符串，还原为明文"""
    if not val or not isinstance(val, str) or not val.startswith("enc:"):
        return val
    try:
        ciphertext = val[4:]
        f = _get_fernet(secret)
        decrypted = f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        return decrypted
    except Exception as e:
        logger.warning(f"解密数据失败（可能密钥已变更）: {e}")
        return val


def encrypt_dict_sensitive_fields(data: Dict[str, Any], secret: str) -> Dict[str, Any]:
    """对字典中的所有敏感字段进行加密"""
    if not data or not secret:
        return data
    cloned = copy.deepcopy(data)
    for path in SENSITIVE_FIELD_PATHS:
        curr = cloned
        for p in path[:-1]:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            else:
                curr = None
                break
        if curr and isinstance(curr, dict) and path[-1] in curr:
            val = curr[path[-1]]
            if val and isinstance(val, str):
                curr[path[-1]] = encrypt_value(val, secret)
    return cloned


def decrypt_dict_sensitive_fields(data: Dict[str, Any], secret: str) -> Dict[str, Any]:
    """对字典中的所有带 enc: 的敏感字段进行解密"""
    if not data or not secret:
        return data
    cloned = copy.deepcopy(data)
    for path in SENSITIVE_FIELD_PATHS:
        curr = cloned
        for p in path[:-1]:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            else:
                curr = None
                break
        if curr and isinstance(curr, dict) and path[-1] in curr:
            val = curr[path[-1]]
            if val and isinstance(val, str):
                curr[path[-1]] = decrypt_value(val, secret)
    return cloned
