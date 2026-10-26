"""Owned local bindings for Inference pipeline API acceptance."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url

_BINDINGS = ("borrowed", "http")
_RESTORE_PREFIX = "aap_inference_pipelines_restore_"


def pipeline_dsn(test_engine, database: str | None = None) -> str:
    """Return the verifier-owned function-only LOGIN URL for this test database."""
    parsed = make_url(
        _role_url(
            "AAP_MODULE_DATA_INFERENCE_PIPELINES_URL",
            "inference_pipelines_client",
        )
    )
    assert parsed.username == "inference_pipelines_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)

    if database is not None:
        if (
            not database.startswith(_RESTORE_PREFIX)
            or not database.endswith("_test")
            or len(database.encode("utf-8")) > 63
        ):
            raise ValueError("Inference pipeline restore database name is not owned")
        parsed = parsed.set(database=database)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def _http_owner(test_engine, tmp_path, *, database: str | None, token: str):
    interpreter_value = os.environ.get("AAP_INFERENCE_INSTALLED_PYTHON")
    interpreter = Path(interpreter_value) if interpreter_value else Path(sys.executable)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise AssertionError("Inference owner Python must be an absolute executable")

    dsn = pipeline_dsn(test_engine, database)
    directory = Path(tempfile.mkdtemp(prefix="inference-pipeline-owner-", dir=tmp_path))
    dsn_file = directory / "database-url"
    token_file = directory / "control-token"
    log_path = directory / "owner.log"
    owner = None
    active_error = None
    try:
        directory.chmod(0o700)
        for path, value in ((dsn_file, dsn), (token_file, token)):
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
        log_path.touch(mode=0o600)

        port = _port()
        environment = {
            "PATH": f"{interpreter.parent}:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "INFERENCE_HOST": "127.0.0.1",
            "INFERENCE_PORT": str(port),
            "INFERENCE_PIPELINES_DATABASE_URL_FILE": str(dsn_file),
            "INFERENCE_CONTROL_TOKEN_FILE": str(token_file),
        }
        assert "PYTHONPATH" not in environment
        assert "INFERENCE_PIPELINES_DATABASE_URL" not in environment
        assert "INFERENCE_CONTROL_TOKEN" not in environment

        owner = await asyncio.to_thread(
            _start,
            [str(interpreter), "-I", "-m", "aap_inference"],
            environment,
            directory,
            log_path,
            port,
            "/health",
        )
        yield owner.origin
    except BaseException as error:
        active_error = error
        raise
    finally:
        cleanup_failed = False
        if owner is not None:
            try:
                await asyncio.to_thread(owner.stop)
            except Exception:
                cleanup_failed = True
            try:
                if owner.process.poll() is None:
                    owner.process.kill()
                    await asyncio.to_thread(owner.process.wait, timeout=5)
                owner.log.close()
                cleanup_failed = cleanup_failed or owner.process.poll() is None
            except Exception:
                cleanup_failed = True

        try:
            log = log_path.read_bytes() if log_path.exists() else b""
        except OSError:
            log = b""
            cleanup_failed = True
        log_leaked_secret = dsn.encode() in log or token.encode() in log
        if log_leaked_secret:
            cleanup_failed = True

        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_failed = True

        if cleanup_failed and active_error is None:
            raise AssertionError("Inference owner cleanup or log redaction failed")
        if cleanup_failed and active_error is not None:
            active_error.add_note("Inference owner cleanup or log redaction failed")


@asynccontextmanager
async def api_pipeline_owner_binding(
    test_engine,
    monkeypatch,
    mode: str,
    tmp_path,
    *,
    database: str | None = None,
):
    """Select borrowed API ownership or an isolated HTTP owner."""
    if mode not in _BINDINGS:
        raise ValueError("Unknown Inference pipeline binding")

    token = secrets.token_urlsafe(32) if mode == "http" else ""
    url = ""

    async with AsyncExitStack() as stack:
        if mode == "http":
            url = await stack.enter_async_context(
                _http_owner(test_engine, tmp_path, database=database, token=token)
            )

        changes = {
            "inference_pipelines_url": url,
            "inference_pipelines_token": token,
            "inference_pipelines_token_file": "",
            "inference_pipelines_timeout_seconds": 30,
        }
        with monkeypatch.context() as scoped:
            for name, value in changes.items():
                scoped.setattr(settings, name, value)
            yield


__all__ = ["_BINDINGS", "api_pipeline_owner_binding", "pipeline_dsn"]
