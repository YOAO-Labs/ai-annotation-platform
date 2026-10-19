from __future__ import annotations

import ast
from pathlib import Path
import tomllib

APP_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_source_has_no_inprocess_app_or_storage_dependencies():
    forbidden = {"app", "boto3", "celery", "redis", "sqlalchemy"}
    imported = set()
    for path in (APP_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
    assert not (forbidden & imported)


def test_client_extra_stays_lightweight_and_server_extras_are_separate():
    project = tomllib.loads((APP_ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    client = " ".join(extras["client"])
    server = " ".join(extras["server"])
    runtime = " ".join(extras["runtime"])
    assert "httpx" in client
    assert all(name not in client for name in ("asyncpg", "fastapi", "uvicorn"))
    assert all(name in server for name in ("asyncpg", "fastapi", "httpx", "uvicorn"))
    assert all(name in runtime for name in ("asyncpg", "httpx"))


def test_image_runs_nonroot_and_contains_no_app_source_mount_or_s3_secrets():
    dockerfile = (APP_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10003:10003" in dockerfile
    assert "COPY apps/api" not in dockerfile
    assert "AWS_ACCESS_KEY_ID" not in dockerfile
    assert "AWS_SECRET_ACCESS_KEY" not in dockerfile
    assert "uv sync --locked --no-dev --extra server --no-editable" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
