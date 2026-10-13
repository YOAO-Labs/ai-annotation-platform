"""Credential-free identities and resource names for local worktree runtimes."""

import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit

BUCKETS = {
    "MINIO_BUCKET": "annotations",
    "MINIO_DATASETS_BUCKET": "datasets",
    "MINIO_BUG_REPORTS_BUCKET": "bug-reports",
    "MINIO_MEDIA_CACHE_BUCKET": "media-cache",
    "MINIO_AUDIT_ARCHIVE_BUCKET": "audit-archive",
    "MINIO_IMPORT_BUCKET": "import",
    "MINIO_EXPORT_BUCKET": "export",
    "MINIO_AVATARS_BUCKET": "avatars",
}
MODES = ("dev", "test", "e2e")


class WorktreeError(ValueError):
    """A credential-free, actionable error safe to display at the CLI boundary."""


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise WorktreeError(
            f"{path.name} 损坏；先核对运行进程和资源归属，不要直接删除身份目录"
        ) from exc
    if not isinstance(value, dict):
        raise WorktreeError(f"{path.name} 不是有效的状态对象")
    return value


def state_path(root: Path, *parts: str) -> Path:
    """Never follow state-directory links into another checkout."""
    path = root.resolve()
    for part in (".worktree", *parts):
        if part in {".", ".."} or "/" in part or "\\" in part:
            raise WorktreeError("无效的工作树状态路径")
        path = path / part
        if path.is_symlink():
            raise WorktreeError("工作树状态不能是符号链接 (symlink)")
    return path


def atomic_json(path: Path, document: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Another complete identity won publication.
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_identity(root: Path, document: dict) -> None:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "id", "root"}
        or document.get("schema") != 1
        or not re.fullmatch(r"[a-f0-9]{16}", str(document.get("id", "")))
    ):
        raise WorktreeError("无效的工作树 identity；拒绝推断资源归属")
    if document["root"] != str(root.resolve()):
        raise WorktreeError("identity 属于另一个 checkout；不要复制 .worktree 目录")


def load_identity(root: Path, *, create: bool = False) -> dict:
    root = root.resolve()
    path = state_path(root, "identity.json")
    if not path.exists() and create:
        document = {"schema": 1, "id": secrets.token_hex(8), "root": str(root)}
        atomic_json(path, document, exclusive=True)
    document = read_state(path)
    validate_identity(root, document)
    return document


def topology(root: Path, identity: dict, mode: str) -> dict:
    validate_identity(root, identity)
    if mode not in MODES:
        raise WorktreeError("环境必须是 dev、test 或 e2e")
    name = f"aap-wt-{identity['id']}-{mode}"
    checkout = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return {
        "id": identity["id"],
        "root": str(root.resolve()),
        "mode": mode,
        "owner": f"aap-worktree:{identity['id']}:{mode}:{checkout}",
        "confirmation": f"{identity['id']}:{mode}",
        "database": f"aap_wt_{identity['id']}_{mode}",
        "redis_name": f"{name}-redis",
        "buckets": {key: f"{name}-{suffix}" for key, suffix in BUCKETS.items()},
        "directory": str(state_path(root, mode)),
    }


def isolated_environment(
    resources: dict,
    *,
    database_url: str,
    migration_database_url: str,
    minio_endpoint: str,
    redis_port: int,
    inherited: dict,
    minio_use_ssl: bool = False,
) -> dict:
    """Return child-only overrides; never persist or print the connection strings."""
    from sqlalchemy.engine import make_url

    local = {"localhost", "127.0.0.1", "::1"}
    runtime = make_url(database_url)
    migration = make_url(migration_database_url or database_url)
    for url in (runtime, migration):
        if url.drivername != "postgresql+asyncpg" or url.host not in local or url.query:
            raise WorktreeError(
                "独立开发环境只接受本机 PostgreSQL asyncpg URL（不带 query）"
            )
    if (runtime.port or 5432) != (migration.port or 5432):
        raise WorktreeError("API 与迁移必须连接同一个本机 PostgreSQL 实例")
    endpoint = urlsplit("http://" + minio_endpoint)
    if endpoint.hostname not in local or endpoint.username or endpoint.path:
        raise WorktreeError("独立开发环境只接受本机 MinIO endpoint")
    if not 1 <= redis_port <= 65535:
        raise WorktreeError("无效的独立 Redis 端口")
    runtime = runtime.set(database=resources["database"])
    migration = migration.set(database=resources["database"])
    directory = Path(resources["directory"])
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    test_url = (
        migration.render_as_string(hide_password=False)
        if resources["mode"] != "dev"
        else "worktree-mode-required://use-exec-mode-test-or-e2e"
    )
    overrides = {
        "DATABASE_URL": runtime.render_as_string(hide_password=False),
        "MIGRATION_DATABASE_URL": migration.render_as_string(hide_password=False),
        "TEST_DATABASE_URL": test_url,
        "PLAYWRIGHT_E2E_DATABASE_URL": test_url,
        "AAP_WORKTREE_MODE": resources["mode"],
        "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": redis_url,
        "CELERY_BROKER_READ_URL": redis_url,
        "CELERY_BROKER_WRITE_URL": redis_url,
        "CELERY_RESULT_BACKEND": redis_url,
        **resources["buckets"],
        "MINIO_ENDPOINT": minio_endpoint,
        "MINIO_PUBLIC_URL": "/minio",
        "MINIO_PROXY_TARGET": f"{'https' if minio_use_ssl else 'http'}://{minio_endpoint}",
        "VITE_WS_HOST": "",  # Same-origin Vite proxy, never a shared checkout's API.
        "DUCKDB_PATH": str(directory / "data" / "analytics.duckdb"),
        "TMPDIR": str(directory / "tmp"),
        "TMP": str(directory / "tmp"),
        "TEMP": str(directory / "tmp"),
        "UV_PROJECT_ENVIRONMENT": str(Path(resources["root"]) / "apps/api/.venv"),
        "ENVIRONMENT": "development",
        "E2E_SEED_ENABLED": "true" if resources["mode"] in {"test", "e2e"} else "false",
        "ALEMBIC_AUTO_UPGRADE": "false",
        "ML_BACKEND_DEFAULT_URL": "",
        "ML_BACKEND_OBSERVE_URLS": "[]",
        "ML_BACKEND_STORAGE_HOST": "",
        "ML_BACKEND_ROUTER_MODE": "off",
        "GPU_ARBITER_MODE": "off",
        "GPU_ARBITER_ROLLOUT_ENABLED": "false",
        "GPU_ARBITER_RESOURCES_JSON": "{}",
        "GPU_ARBITER_COLLECTOR_DATABASE_URL_FILE": "",
        "SMTP_HOST": "",
        "SENTRY_DSN": "",
    }
    # Pydantic settings are case-insensitive; remove lower-case inherited aliases too.
    return {
        **{
            key: value
            for key, value in inherited.items()
            if key.upper() not in overrides
        },
        **overrides,
    }


def frontend_environment(environment: dict) -> dict:
    """Vite/supervisor need OS/public configuration, not application credentials."""
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "CI",
        "FORCE_COLOR",
        "NO_COLOR",
        "NODE_OPTIONS",
        "PNPM_HOME",
        "COREPACK_HOME",
        "API_PROXY_TARGET",
        "MINIO_PROXY_TARGET",
        "PORT",
    }
    return {
        key: value
        for key, value in environment.items()
        if key in allowed or key.startswith("VITE_")
    }


def application_environment(environment: dict) -> dict:
    """Remove migration/test credentials before launching application processes."""
    return {
        **environment,
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "PLAYWRIGHT_E2E_DATABASE_URL": "",
    }
