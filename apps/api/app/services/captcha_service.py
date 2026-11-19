"""Cloudflare Turnstile CAPTCHA verification.

`turnstile_enabled=False` 时 short-circuit 返 True，本地开发与 CI 不需配置 key。
production 启用后向 challenges.cloudflare.com/turnstile/v0/siteverify POST
form-encoded `secret + response (+ remoteip)`，3s 超时；网络异常或超时一律返 False
（fail-closed），让前端拿到 captcha_failed。
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger("anno-api.captcha")

_TIMEOUT_SECONDS = 3.0


async def verify_turnstile_token(
    token: Optional[str],
    remote_ip: Optional[str] = None,
) -> bool:
    """校验 Turnstile token。返回 True 即放行。"""
    if not settings.turnstile_enabled:
        return True

    if not settings.turnstile_secret_key:
        logger.warning(
            "turnstile_enabled=True 但 turnstile_secret_key 未配置，拒绝放行"
        )
        return False

    if not token:
        return False

    payload = {
        "secret": settings.turnstile_secret_key,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(settings.turnstile_verify_url, data=payload)
        if resp.status_code != 200:
            logger.warning(
                "turnstile siteverify HTTP %s: %s", resp.status_code, resp.text[:200]
            )
            return False
        body = resp.json()
        return bool(body.get("success"))
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        logger.warning("turnstile siteverify network error: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 — 兜底防 JSON parse error 等
        logger.exception("turnstile siteverify unexpected error: %s", exc)
        return False
