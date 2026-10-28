"""Private first-party backend Bearer authentication at the ASGI boundary."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from aap_protocol_v2.backend_auth import BACKEND_SERVICES
from aap_protocol_v2.managed_credential import CredentialFileError, read_credential


def manual_credential(provider: str) -> str:
    legacy = (
        os.environ.get("YOLO_MANAGED_MODELS_TOKEN", "") if provider == "yolo" else ""
    )
    return legacy or os.environ.get("AAP_BACKEND_AUTH_TOKEN", "")


def backend_credential(provider: str) -> str:
    manual = manual_credential(provider)
    if manual:
        return manual
    try:
        return read_credential(
            Path(os.environ.get("AAP_BACKEND_AUTH_DIR", "/run/aap-inference-auth"))
        )
    except (OSError, CredentialFileError):
        return ""


def backend_auth_headers(provider: str) -> dict[str, str]:
    token = backend_credential(provider)
    if len(token) < 32:
        raise RuntimeError(
            "Backend credential is unavailable; initialize deployment credentials first"
        )
    return {"Authorization": f"Bearer {token}"}


def require_external_bearer(request, expected_token: str) -> None:
    """Authenticate an external instance token independently of provider identity."""
    from fastapi import HTTPException

    supplied = request.headers.getlist("authorization")
    if (
        len(expected_token) < 32
        or len(supplied) != 1
        or not secrets.compare_digest(
            supplied[0].encode("utf-8"), f"Bearer {expected_token}".encode("utf-8")
        )
    ):
        raise HTTPException(status_code=403, detail="Backend authentication failed")


class BackendBearerMiddleware:
    def __init__(self, app, provider: str):
        self.app = app
        self.provider = provider

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse

        if scope["type"] == "http" and scope["path"] not in {"/health", "/metrics"}:
            expected = backend_credential(self.provider)
            supplied = [
                value
                for name, value in scope["headers"]
                if name.lower() == b"authorization"
            ]
            if (
                len(expected) < 32
                or len(supplied) != 1
                or not secrets.compare_digest(
                    supplied[0], f"Bearer {expected}".encode()
                )
            ):
                await JSONResponse(
                    {"detail": "Backend authentication failed"}, status_code=403
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_backend_auth(app, provider: str) -> None:
    if provider not in BACKEND_SERVICES:
        raise ValueError("Unknown first-party backend")
    app.add_middleware(BackendBearerMiddleware, provider=provider)

    @app.get("/managed-auth")
    def managed_auth_status():
        return {
            "provider": provider,
            "mode": "manual" if manual_credential(provider) else "managed",
        }
