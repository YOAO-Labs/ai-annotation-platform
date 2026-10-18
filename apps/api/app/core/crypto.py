"""v0.11.14 · 连接器凭据对称加密（Fernet）。

存储连接器（外部 S3 / SFTP）的密钥落库前用 Fernet 加密为密文，
读出时在 service 层内解密、用完即弃，绝不进任何 response schema。

加密 key 由独立环境变量 ``CONNECTOR_ENCRYPTION_KEY`` 提供（与 JWT ``secret_key`` 隔离），
是一把 Fernet key（32 字节 url-safe base64）。生成方式：

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

未配置时，任何加解密调用抛 ``ConnectorCryptoNotConfigured``，由 API 层转 503，
提示运维先配置——连接器创建/测试/导入在缺 key 时一律拒绝，而非静默降级。
"""

from __future__ import annotations

from aap_assets_source.connectors.crypto import (
    ConnectorCryptoError as PortableConnectorCryptoError,
    ConnectorCryptoNotConfigured as PortableConnectorCryptoNotConfigured,
    decrypt_secret as _decrypt_secret,
    encrypt_secret as _encrypt_secret,
)

from app.config import settings


class ConnectorCryptoNotConfigured(RuntimeError):
    """CONNECTOR_ENCRYPTION_KEY 未配置或非法。"""


class ConnectorCryptoError(RuntimeError):
    """密文损坏 / key 不匹配，无法解密。"""


def _key() -> str:
    key = settings.connector_encryption_key
    if not key:
        raise ConnectorCryptoNotConfigured(
            "CONNECTOR_ENCRYPTION_KEY 未配置，无法加解密连接器凭据"
        )
    return key


def encrypt_secret(secret: dict) -> bytes:
    """把密钥 dict（如 {access_key, secret_key} / {password}）加密为密文字节。"""
    try:
        return _encrypt_secret(secret, _key())
    except PortableConnectorCryptoNotConfigured as exc:
        raise ConnectorCryptoNotConfigured(str(exc)) from exc
    except PortableConnectorCryptoError as exc:
        raise ConnectorCryptoError(str(exc)) from exc


def decrypt_secret(token: bytes) -> dict:
    """解密回密钥 dict。密文损坏 / key 不匹配抛 ConnectorCryptoError。"""
    try:
        return _decrypt_secret(token, _key())
    except PortableConnectorCryptoNotConfigured as exc:
        raise ConnectorCryptoNotConfigured(str(exc)) from exc
    except PortableConnectorCryptoError as exc:
        raise ConnectorCryptoError(
            "连接器凭据解密失败（密文损坏或密钥不匹配）"
        ) from exc
