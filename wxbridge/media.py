"""
WXBridge 媒体加密工具

提供 AES-128-ECB 加解密、密钥生成、MD5 计算等工具函数，
供 ilink_client 在媒体上传/下载时使用。

依赖：cryptography（可选）。未安装时调用任何函数均抛 ImportError。
安装：pip install 'wxbridge[media]' 或 pip install cryptography
"""
from __future__ import annotations

import base64
import hashlib
import os

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


def _require_cryptography() -> None:
    """检查 cryptography 包是否可用，不可用时给出友好错误"""
    try:
        import cryptography  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "媒体功能需要 cryptography 库。请运行：\n"
            "    pip install 'wxbridge[media]'\n"
            "或者：\n"
            "    pip install cryptography"
        ) from e


def generate_aes_key() -> bytes:
    """生成 16 字节随机 AES-128 密钥"""
    return os.urandom(16)


def aes_key_to_b64(key: bytes) -> str:
    """将 AES 密钥编码为 Base64 字符串（API 传输格式）"""
    return base64.b64encode(key).decode()


def aes_key_from_b64(b64_key: str) -> bytes:
    """将 Base64 字符串解码为 AES 密钥字节"""
    return base64.b64decode(b64_key)


def aes_encrypt(data: bytes, key: bytes) -> bytes:
    """
    AES-128-ECB 加密（PKCS7 填充）

    Args:
        data: 明文字节
        key:  16 字节 AES 密钥

    Returns:
        加密后的字节（含 PKCS7 填充）
    """
    _require_cryptography()
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding

    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_decrypt(data: bytes, key: bytes) -> bytes:
    """
    AES-128-ECB 解密（去除 PKCS7 填充）

    Args:
        data: 加密字节
        key:  16 字节 AES 密钥

    Returns:
        解密后的明文字节
    """
    _require_cryptography()
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding

    cipher = Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def md5_bytes(data: bytes) -> str:
    """计算字节数据的 hex MD5 摘要"""
    return hashlib.md5(data).hexdigest()  # noqa: S324
