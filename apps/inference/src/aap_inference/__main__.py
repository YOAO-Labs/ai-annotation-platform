"""Launch optional independent Inference owner capabilities."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .backend_probe_manager import BackendProbeManager
from .backend_probe_postgres import BackendProbePostgresData
from .pipeline_manager import PipelineManager
from .pipeline_postgres import PipelinePostgresData
from .pipeline_server import PipelineHttpServer


def _environment_value(name: str) -> str | None:
    direct = os.environ.get(name)
    file_path = os.environ.get(f"{name}_FILE")
    if direct is not None and file_path is not None:
        raise ValueError(f"{name} and {name}_FILE cannot both be set")
    if file_path is not None:
        try:
            return Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ValueError(f"Unable to read {name}_FILE") from None
    return direct


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"Invalid {name}")
    significant = raw.lstrip("0") or "0"
    if len(significant) > len(str(maximum)):
        raise ValueError(f"Invalid {name}")
    value = int(significant)
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


async def run() -> None:
    dsn = _environment_value("INFERENCE_PIPELINES_DATABASE_URL")
    token = _environment_value("INFERENCE_CONTROL_TOKEN")
    probe_dsn = _environment_value("INFERENCE_BACKEND_PROBE_DATABASE_URL")
    probe_token = _environment_value("INFERENCE_BACKEND_PROBE_CONTROL_TOKEN")
    if (probe_dsn is None) != (probe_token is None):
        raise ValueError(
            "INFERENCE_BACKEND_PROBE_DATABASE_URL and "
            "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN must be configured together"
        )
    host = os.environ.get("INFERENCE_HOST", "127.0.0.1")
    if not host or any(character.isspace() for character in host):
        raise ValueError("Invalid INFERENCE_HOST")
    port = _integer_setting("INFERENCE_PORT", 8768, 1, 65_535)
    max_concurrency = _integer_setting("INFERENCE_MAX_CONCURRENCY", 8, 1, 64)

    data = None
    manager = None
    if dsn and token:
        data = PipelinePostgresData(dsn)
        manager = PipelineManager(data)
    probe_data = None
    probe_manager = None
    if probe_dsn is not None and probe_token is not None:
        probe_data = BackendProbePostgresData(probe_dsn)
        probe_manager = BackendProbeManager(probe_data)

    server = None
    try:
        server = PipelineHttpServer(
            manager,
            data,
            token,
            host=host,
            port=port,
            max_concurrency=max_concurrency,
            backend_probe_manager=probe_manager,
            backend_probe_control_token=probe_token,
        )
        await server.serve_forever()
    finally:
        try:
            if server is not None:
                await server.close()
        finally:
            try:
                if data is not None:
                    await data.close()
            finally:
                if probe_data is not None:
                    await probe_data.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
