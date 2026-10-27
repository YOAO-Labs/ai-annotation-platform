"""Deployment-pinned first-party credentials; never enroll arbitrary registry URLs."""

from pathlib import Path

import httpx
from aap_protocol_v2.backend_auth import (
    BACKEND_SERVICES,
    managed_provider,
    normalized_backend_url,
    validate_backend_urls,
)
from aap_protocol_v2.managed_credential import (
    CredentialFileError,
    ensure_credential,
    read_credential,
)
from fastapi import HTTPException

from app.config import settings


def configured_backends() -> dict[str, str]:
    return validate_backend_urls(settings.ml_managed_backend_urls)


def provider_for_url(url: str) -> str | None:
    normalized = normalized_backend_url(url)
    return next(
        (
            key
            for key, target in configured_backends().items()
            if normalized and normalized_backend_url(target) == normalized
        ),
        None,
    )


def is_managed_url(url: str, provider: str) -> bool:
    return provider in BACKEND_SERVICES and provider_for_url(url) == provider


def credential_directory(provider: str) -> Path:
    if provider not in BACKEND_SERVICES:
        raise ValueError("Unknown first-party backend")
    return Path(settings.ml_managed_auth_dir) / provider


def initialize_default_credentials() -> dict[str, str]:
    """Create independent defaults without networking, DB writes, or rotation."""
    results = {}
    for provider in configured_backends():
        try:
            ensure_credential(credential_directory(provider))
            results[provider] = "ready"
        except (OSError, CredentialFileError):
            results[provider] = "invalid"
    return results


def local_managed_headers(url: str) -> dict[str, str]:
    """Read existing defaults only for an unregistered, deployment-pinned target."""
    provider = provider_for_url(url)
    if provider is None:
        return {}
    try:
        token = read_credential(credential_directory(provider))
    except (OSError, CredentialFileError):
        return {}
    return {"Authorization": f"Bearer {token}"}


async def verify_managed_auth(url: str, token: str, provider: str) -> None:
    if not is_managed_url(url, provider):
        raise HTTPException(
            422, "系统托管仅支持部署指定的内置服务地址；其他服务请使用手动 Token。"
        )
    try:
        async with httpx.AsyncClient(
            timeout=settings.ml_health_timeout, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(
                f"{url.rstrip('/')}/managed-auth",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 200 and response.json() == {
            "provider": provider,
            "mode": "managed",
        }:
            return
    except (httpx.RequestError, ValueError):
        pass
    raise HTTPException(
        422,
        "无法验证内置服务系统托管认证。请确认服务已升级并挂载自己的共享凭据目录；"
        "若服务端已有手动 Token，请继续使用手动认证。"
        "本地凭据已保留，可直接重试初始化或保存。",
    )


async def initialize_managed_auth(
    url: str, supplied_token: str | None, method: str
) -> str:
    provider = managed_provider(method)
    if (
        provider is None
        or not is_managed_url(url, provider)
        or supplied_token is not None
    ):
        raise HTTPException(
            422, "系统托管须选择部署指定的内置服务，且不能自行填写 Token。"
        )
    try:
        token = ensure_credential(credential_directory(provider))
    except (OSError, CredentialFileError):
        raise HTTPException(
            422,
            "无法初始化本地凭据：请检查共享目录归属、私有权限和文件完整性；现有文件不会被覆盖。",
        ) from None
    await verify_managed_auth(url, token, provider)
    return token
