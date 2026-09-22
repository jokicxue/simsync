import base64
import os
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class DataCipher:
    """
    基于 AES-256-GCM 的数据库敏感字段加密器
    使用用户密码作为主密钥派生加密，实现静态存储加密 (Encryption at Rest)
    """

    PREFIX = "ENC:v1:"

    def __init__(self, password: Optional[str] = None):
        self.password = password or ""
        self._key_cache = {}

    def _get_key(self, salt: bytes) -> bytes:
        if not self.password or not HAS_CRYPTO:
            return b""
        if salt not in self._key_cache:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            self._key_cache[salt] = kdf.derive(self.password.encode("utf-8"))
        return self._key_cache[salt]

    def encrypt(self, text: Optional[str]) -> str:
        """加密文本，如果未设置密码或缺少加密库则原样返回"""
        if not text or not self.password or not HAS_CRYPTO:
            return text or ""

        try:
            salt = os.urandom(16)
            nonce = os.urandom(12)
            key = self._get_key(salt)

            aesgcm = AESGCM(key)
            ciphertext = aesgcm.encrypt(nonce, text.encode("utf-8"), None)

            # 打包: salt (16B) + nonce (12B) + ciphertext
            payload = salt + nonce + ciphertext
            b64_str = base64.b64encode(payload).decode("ascii")
            return f"{self.PREFIX}{b64_str}"
        except Exception:
            return text

    def decrypt(self, text: Optional[str]) -> str:
        """解密文本，兼容未加密的明文"""
        if not text:
            return ""

        # 如果不是我们加密的格式，说明是明文，直接返回
        if not text.startswith(self.PREFIX):
            return text

        if not self.password or not HAS_CRYPTO:
            return "[加密内容: 需密码解密]"

        try:
            b64_str = text[len(self.PREFIX) :]
            payload = base64.b64decode(b64_str.encode("ascii"))

            salt = payload[:16]
            nonce = payload[16:28]
            ciphertext = payload[28:]

            key = self._get_key(salt)
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8", errors="replace")
        except Exception:
            return "[解密失败: 密码错误或数据损坏]"
