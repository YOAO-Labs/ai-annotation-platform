"""Independent poller for versioned Recovery and invitation mail delivery."""

from __future__ import annotations

import asyncio

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_postgres import MailSettingsPostgresData
from aap_identity.mail_v2_contract import MAIL_V2_POLICY
from aap_identity.mail_v2_postgres import MailV2PostgresData

from .mail_config_v2 import MailV2WorkerSettings
from .mail_worker import MailExecutor


def _fallbacks(settings: MailV2WorkerSettings) -> dict[str, object]:
    return {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password.get_secret_value(),
        "smtp_from": settings.smtp_from,
    }


def create_app(
    settings: MailV2WorkerSettings | None = None,
    *,
    data: MailV2PostgresData | None = None,
    settings_data=None,
    executor: MailExecutor | None = None,
):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    settings = settings or MailV2WorkerSettings()
    data = data or MailV2PostgresData(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )
    settings_data = settings_data or MailSettingsPostgresData(
        settings.settings_database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )

    async def policy():
        return await settings_data.mail_delivery_policy(_fallbacks(settings))

    executor = executor or MailExecutor(
        data,
        policy,
        settings.encryption_key,
        lease_seconds=settings.lease_seconds,
        retry_delay_seconds=settings.retry_delay_seconds,
    )
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_app):
        stop.clear()
        task = asyncio.create_task(poll_queue(), name="identity-mail-v2-poll")
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def poll_queue() -> None:
        while not stop.is_set():
            try:
                result = await executor.run_once()
                if result.get("state") in {"idle", "stale", "unavailable"}:
                    await asyncio.sleep(settings.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Driver and SMTP exceptions may contain connection details.
                await asyncio.sleep(settings.poll_interval_seconds)

    app = FastAPI(
        title="Identity mail v2 worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            from aap_identity.recovery_crypto import mail_encryption_id

            async with asyncio.timeout(settings.command_timeout_seconds + 1):
                native = await data.mail_policy()
                await policy()
                if native.policy != MAIL_V2_POLICY:
                    raise IdentityUnavailable()
                mail_encryption_id(executor._key_value())
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app


def main() -> None:
    import uvicorn

    try:
        settings = MailV2WorkerSettings()
        app = create_app(settings)
    except Exception:
        raise SystemExit("Mail v2 worker configuration invalid") from None
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
