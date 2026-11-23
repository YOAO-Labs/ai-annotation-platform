"""Cold HTTP processes for the restricted Workspace consumer acceptance."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

import httpx
from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import Process, _port, _start
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url


ROOT = Path(__file__).resolve().parents[3]
_API = """
import sys
from unittest.mock import patch

import uvicorn

with patch('pydantic_settings.sources.DotEnvSettingsSource._read_env_files', return_value={}):
    from app.main import app

uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), access_log=False,
            timeout_graceful_shutdown=3)
"""
_INFERENCE_PROVIDERS = {
    "backend_probe": ("yoao_inference_backend_probe_client", "CONTROL_TOKEN"),
    "health_observation": ("yoao_inference_health_observation_client", "TOKEN"),
    "registry_ingress": ("yoao_inference_registry_ingress_client", "TOKEN"),
}


class WorkspaceModuleStack:
    """Run the normal API and one credential owner for the three Inference channels."""

    def __init__(
        self,
        test_engine,
        directory: Path,
        *,
        buckets: dict[str, str],
        api_s3_credentials: tuple[str, str],
        api_secret: str,
        inference_token: str,
    ) -> None:
        assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
        _verify_postgres(test_engine.url)
        redis_url = _verify_redis().render_as_string(hide_password=False)
        self._test_url = test_engine.url
        self._database = test_engine.url.database
        assert self._database and self._database.endswith("_test")
        self._directory = directory
        self._inference_tokens = {
            "backend_probe": inference_token,
            "health_observation": secrets.token_urlsafe(32),
            "registry_ingress": secrets.token_urlsafe(32),
        }
        self._urls = {
            variable: make_url(_role_url(f"AAP_MODULE_DATA_{name}_URL", role))
            for variable, name, role in (
                ("DATABASE_URL", "IDENTITY_API_CONSUMER", "identity_api_consumer"),
                (
                    "IDENTITY_AUTH_DATABASE_URL",
                    "IDENTITY_AUTH",
                    "yoao_identity_auth_client",
                ),
                (
                    "IDENTITY_BRIEFS_DATABASE_URL",
                    "IDENTITY_BRIEFS",
                    "yoao_identity_briefs_client",
                ),
                *(
                    (
                        f"INFERENCE_{capability.upper()}_DATABASE_URL",
                        f"INFERENCE_{capability.upper()}",
                        role,
                    )
                    for capability, (role, _) in _INFERENCE_PROVIDERS.items()
                ),
            )
        }
        for url in self._urls.values():
            assert (url.host, url.port, url.database) == (
                test_engine.url.host,
                test_engine.url.port,
                self._database,
            )
        access_key, secret_key = api_s3_credentials
        self._common_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self._api_env = {
            **self._common_env,
            "PYTHONPATH": str(ROOT / "apps/api"),
            "OPENBLAS_NUM_THREADS": "1",
            "ENVIRONMENT": "development",
            "DEBUG": "false",
            "SECRET_KEY": api_secret,
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "REDIS_URL": redis_url,
            "CELERY_BROKER_URL": redis_url,
            "AUDIT_ASYNC": "true",
            "MINIO_ENDPOINT": settings.minio_endpoint,
            "MINIO_USE_SSL": str(settings.minio_use_ssl),
            "MINIO_ACCESS_KEY": access_key,
            "MINIO_SECRET_KEY": secret_key,
            "AWS_EC2_METADATA_DISABLED": "true",
            **buckets,
        }
        self._secrets = tuple(
            value
            for value in {
                api_secret,
                *self._inference_tokens.values(),
                access_key,
                secret_key,
                make_url(redis_url).password,
                *(url.password for url in self._urls.values()),
            }
            if value
        )
        self._api: Process | None = None
        self._inference: Process | None = None
        self._inference_port = 0
        self._runtime: tempfile.TemporaryDirectory | None = None

    def _dsn(self, variable: str) -> str:
        url = self._urls[variable].set(database=self._database)
        if variable != "DATABASE_URL":
            url = url.set(drivername="postgresql")
        return url.render_as_string(hide_password=False)

    @property
    def api_origin(self) -> str:
        assert self._api is not None
        return self._api.origin

    @property
    def inspection_evidence(self) -> dict:
        assert self._api is not None and self._inference is not None
        return {
            "database": self._database,
            "api": {
                "pid": self._api.process.pid,
                "database_role": self._urls["DATABASE_URL"].username,
                "identity_auth_role": self._urls["IDENTITY_AUTH_DATABASE_URL"].username,
                "identity_briefs_role": self._urls[
                    "IDENTITY_BRIEFS_DATABASE_URL"
                ].username,
            },
            "inference": {
                "pid": self._inference.process.pid,
                "database_roles": {
                    capability: role
                    for capability, (role, _) in _INFERENCE_PROVIDERS.items()
                },
            },
        }

    async def start(self) -> None:
        assert self._runtime is None
        _verify_postgres(self._test_url)
        _verify_redis()
        self._runtime = tempfile.TemporaryDirectory(dir=self._directory)
        runtime = Path(self._runtime.name)
        for capability, token in self._inference_tokens.items():
            token_file = runtime / f"inference-{capability}-token"
            token_file.touch(mode=0o600)
            token_file.write_text(token, encoding="utf-8")
        self._inference_port = _port()
        await self.start_inference()
        api_port = _port()
        environment = {
            **self._api_env,
            **{
                name: self._dsn(name)
                for name in (
                    "DATABASE_URL",
                    "IDENTITY_AUTH_DATABASE_URL",
                    "IDENTITY_BRIEFS_DATABASE_URL",
                )
            },
            "DUCKDB_PATH": str(runtime / "analytics.duckdb"),
            "INFERENCE_BACKEND_PROBE_URL": f"http://127.0.0.1:{self._inference_port}",
            "INFERENCE_BACKEND_PROBE_TOKEN_FILE": str(
                runtime / "inference-backend_probe-token"
            ),
        }
        self._api = await asyncio.to_thread(
            _start,
            [sys.executable, "-c", _API, str(api_port)],
            environment,
            runtime,
            runtime / "api.log",
            api_port,
            "/health/db",
        )
        async with httpx.AsyncClient(
            base_url=self.api_origin, trust_env=False
        ) as client:
            for component in ("redis", "minio"):
                response = await client.get(f"/health/{component}")
                assert response.status_code == 200, response.text

    async def start_inference(self) -> None:
        assert self._runtime is not None and self._inference is None
        runtime = Path(self._runtime.name)
        self._inference = await asyncio.to_thread(
            _start,
            [sys.executable, "-m", "yoao_inference"],
            {
                **self._common_env,
                "INFERENCE_HOST": "127.0.0.1",
                "INFERENCE_PORT": str(self._inference_port),
                "INFERENCE_REQUIRED_CAPABILITIES": ",".join(_INFERENCE_PROVIDERS),
                **{
                    f"INFERENCE_{capability.upper()}_DATABASE_URL": self._dsn(
                        f"INFERENCE_{capability.upper()}_DATABASE_URL"
                    )
                    for capability in _INFERENCE_PROVIDERS
                },
                **{
                    f"INFERENCE_{capability.upper()}_{token_key}_FILE": str(
                        runtime / f"inference-{capability}-token"
                    )
                    for capability, (_, token_key) in _INFERENCE_PROVIDERS.items()
                },
            },
            runtime,
            runtime / "inference.log",
            self._inference_port,
            "/health",
        )

    async def stop_inference(self) -> None:
        if self._inference is not None:
            await asyncio.to_thread(self._inference.stop)
            self._inference = None

    async def stop(self) -> None:
        try:
            if self._api is not None:
                await asyncio.to_thread(self._api.stop)
                self._api = None
        finally:
            await self.stop_inference()
            if self._runtime is not None:
                self._runtime.cleanup()
                self._runtime = None

    async def switch_database(self, database: str) -> None:
        assert re.fullmatch(r"aap_workspace_module_restore_[0-9a-f]{10}_test", database)
        await self.stop()
        self._database = database
        await self.start()

    def capture_diagnostics(self) -> str:
        if self._runtime is None:
            return ""
        logs = []
        for path in sorted(Path(self._runtime.name).glob("*.log")):
            tail = path.read_text(encoding="utf-8", errors="replace")
            logs.append(f"{path.name}:\n{tail}")
        value = "\n".join(logs)
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "<redacted>")
        return re.sub(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
            "<redacted-jwt>",
            value,
        )[-12_000:]
