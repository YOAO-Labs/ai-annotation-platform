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
    "MINIO_TRAINING_BUCKET": "training",
    "MINIO_INPUTS_BUCKET": "accepted-inputs",
}
MODES = ("dev", "test", "e2e")
FIXTURE_ORIGINAL_ENV_KEYS = frozenset(
    "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
    for suffix in (
        "ENDPOINT",
        "BUCKET",
        "ACCESS_KEY",
        "SECRET_KEY",
        "ACCESS_KEY_FILE",
        "SECRET_KEY_FILE",
    )
)
_INSIGHTS_REFRESH_SELECTORS = (
    "INSIGHTS_REFRESH_DATABASE_URL",
    "INSIGHTS_REFRESH_DATABASE_URL_FILE",
)


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
                pass
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reject_legacy_worktree_environment(environment):
    if {"AAP_WORKTREE_MODE", "AAP_WORKTREE_RESOURCE_ROOT"}.intersection(
        key.upper() for key in environment
    ):
        raise WorktreeError(
            "检测到旧工作树环境变量；请退出旧进程，以干净环境启动，不会回退或接管资源"
        )


def validate_identity(root: Path, document: dict) -> None:
    if isinstance(document, dict) and document.get("schema") == 1:
        raise WorktreeError(
            "旧工作树 identity；请用匹配的旧工具盘点及精确退役，不能原地改名"
        )
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "id", "root"}
        or type(document.get("schema")) is not int
        or document["schema"] != 2
        or not re.fullmatch(r"[a-f0-9]{16}", str(document.get("id", "")))
    ):
        raise WorktreeError("无效的工作树 identity；拒绝推断资源归属")
    if document["root"] != str(root.resolve()):
        raise WorktreeError("identity 属于另一个 checkout；不要复制 .worktree 目录")


def load_identity(root: Path, *, create: bool = False) -> dict:
    root = root.resolve()
    path = state_path(root, "identity.json")
    if not path.exists() and create:
        if any(state_path(root, mode).exists() for mode in MODES):
            raise WorktreeError(
                "缺少 identity 但存在模式状态；拒绝创建新身份或接管资源"
            )
        document = {"schema": 2, "id": secrets.token_hex(8), "root": str(root)}
        atomic_json(path, document, exclusive=True)
    document = read_state(path)
    validate_identity(root, document)
    return document


def worktree_mode(environment: dict) -> str | None:
    """Reject ambiguous selectors instead of silently entering standalone mode."""
    reject_legacy_worktree_environment(environment)
    for key in environment:
        if key.upper() == "YOAO_WORKTREE_MODE" and key != "YOAO_WORKTREE_MODE":
            raise WorktreeError("工作树模式变量必须使用规范名称 YOAO_WORKTREE_MODE")
    mode = environment.get("YOAO_WORKTREE_MODE")
    if mode is not None and mode not in MODES:
        raise WorktreeError("无效的 YOAO_WORKTREE_MODE")
    return mode


def validate_launch_mode(root: Path, mode: str) -> None:
    from dotenv import dotenv_values

    if worktree_mode(os.environ) not in (None, mode):
        raise WorktreeError("启动模式与调用方工作树模式不一致")
    for name in (".env", ".env.local"):
        path = root / name
        if path.is_file() and any(
            key.upper() in {"AAP_WORKTREE_MODE", "YOAO_WORKTREE_MODE"}
            for key in dotenv_values(path, interpolate=False)
        ):
            raise WorktreeError(f"{name} 不能配置由启动器分配的工作树模式变量")


def topology(root: Path, identity: dict, mode: str) -> dict:
    validate_identity(root, identity)
    if mode not in MODES:
        raise WorktreeError("环境必须是 dev、test 或 e2e")
    name = f"yoao-wt-{identity['id']}-{mode}"
    checkout = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return {
        "id": identity["id"],
        "root": str(root.resolve()),
        "mode": mode,
        "owner": f"yoao-worktree:{identity['id']}:{mode}:{checkout}",
        "confirmation": f"{identity['id']}:{mode}",
        "database": f"yoao_wt_{identity['id']}_{mode}",
        "redis_name": f"{name}-redis",
        "buckets": {key: f"{name}-{suffix}" for key, suffix in BUCKETS.items()},
        "directory": str(state_path(root, mode)),
    }


def _validate_environment_sources(
    resources: dict,
    *,
    database_url: str,
    migration_database_url: str,
    minio_endpoint: str,
    redis_port: int,
    inherited: dict,
    minio_use_ssl: bool,
    retirement: bool = False,
):
    """Validate connection sources without creating a child environment."""
    from sqlalchemy.engine import make_url

    if worktree_mode(inherited) not in (None, resources["mode"]):
        raise WorktreeError("继承的工作树模式与资源清单不一致")

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
    if endpoint.username or endpoint.path:
        raise WorktreeError("独立开发环境只接受本机 MinIO endpoint")
    if retirement and (resources["mode"] != "test" or endpoint.hostname in local):
        raise WorktreeError("仅可退休使用共享 bootstrap 的测试环境")
    if endpoint.hostname not in local:
        selector = inherited.get("AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER")
        if (
            resources["mode"] != "test"
            or not selector
            or selector != os.environ.get("AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER")
        ):
            raise WorktreeError("独立开发环境只接受本机 MinIO endpoint")
        from worktree_resources import (
            _verified_finished_shared_bootstrap_minio,
            verified_shared_bootstrap_minio,
        )

        scheme = "https" if minio_use_ssl else "http"
        verify = (
            _verified_finished_shared_bootstrap_minio
            if retirement
            else verified_shared_bootstrap_minio
        )
        if verify(f"{scheme}://{minio_endpoint}") is None:
            raise WorktreeError("独立开发环境只接受已验证的测试 MinIO endpoint")
    if not 1 <= redis_port <= 65535:
        raise WorktreeError("无效的独立 Redis 端口")
    return runtime, migration


def isolated_module_environment() -> dict[str, str]:
    """Beat, API and local workers must not rediscover shared .env credentials."""
    empty_selectors = {
        *_INSIGHTS_REFRESH_SELECTORS,
        "ASSETS_MEDIA_URL",
        "ASSETS_MEDIA_TOKEN",
        "ASSETS_CATALOG_URL",
        "ASSETS_CATALOG_TOKEN",
        "ASSETS_CATALOG_TOKEN_FILE",
        "ASSETS_CATALOG_TIMEOUT_SECONDS",
        "ASSETS_SOURCE_URL",
        "ASSETS_SOURCE_NETWORK",
        "ASSETS_SOURCE_SAVED_IO_SPOOL_DIR",
        "ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE",
        "ASSETS_MASK_CONTENT_URL",
        "ASSETS_MASK_CONTENT_TOKEN",
        "ASSETS_MASK_CLEANUP_URL",
        "ASSETS_MASK_CLEANUP_TOKEN",
        "LABELING_MASK_GC_URL",
        "LABELING_MASK_GC_TOKEN",
        "IDENTITY_URL",
        "IDENTITY_TOKEN",
        "IDENTITY_ADMISSION_TOKEN",
        "IDENTITY_ADMISSION_TOKEN_FILE",
        "IDENTITY_RUNTIME_TOKEN_FILE",
        "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE",
        "IDENTITY_RUNTIME_SIGNING_SECRET_FILE",
        "IDENTITY_EMBEDDED_SIGNING_SECRET",
        "IDENTITY_EMBEDDED_SIGNING_SECRET_FILE",
        "IDENTITY_RECOVERY_MAIL_KEY",
        "IDENTITY_RECOVERY_MAIL_KEY_FILE",
        "IDENTITY_MAIL_ENCRYPTION_KEY",
        "IDENTITY_MAIL_V2_ENCRYPTION_KEY",
        "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE",
        "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE",
        "NOTIFICATION_SOCKET_URL",
        "NOTIFICATION_PUBLICATION_URL",
        "NOTIFICATION_SOCKET_IDENTITY_URL",
        "NOTIFICATION_SOCKET_IDENTITY_TOKEN",
        "NOTIFICATION_SOCKET_IDENTITY_TOKEN_FILE",
        "WORKSPACE_TOKEN",
        "WORKSPACE_TOKEN_FILE",
        "WORKSPACE_CONTROL_TOKEN",
        "WORKSPACE_CONTROL_TOKEN_FILE",
        "AAP_JOBS_QUEUE_RUNTIME_IMAGE",
        "AAP_JOBS_DEVELOPMENT_NETWORK",
    }
    empty_selectors.update(
        f"{capability}_{suffix}"
        for capability, suffixes in (
            (
                "JOBS_QUEUE",
                (
                    "URL",
                    "TOKEN",
                    "TOKEN_FILE",
                    "BROKER_URL",
                    "BROKER_URL_FILE",
                    "HOST",
                    "PORT",
                ),
            ),
            (
                "JOBS_DISPATCH_RELAY",
                (
                    "ENABLED",
                    "DATABASE_URL",
                    "DATABASE_URL_FILE",
                    "BATCH_SIZE",
                    "LEASE_SECONDS",
                    "POLL_SECONDS",
                ),
            ),
            (
                "LABELING_CLASS_QUERY",
                (
                    "URL",
                    "DATABASE_URL",
                    "DATABASE_URL_FILE",
                    "CONTROL_TOKEN",
                    "CONTROL_TOKEN_FILE",
                    "TIMEOUT_SECONDS",
                ),
            ),
            (
                "LABELING_VIDEO_WRITER",
                (
                    "URL",
                    "DATABASE_URL",
                    "DATABASE_URL_FILE",
                    "CONTROL_TOKEN",
                    "CONTROL_TOKEN_FILE",
                    "TIMEOUT_SECONDS",
                ),
            ),
            (
                "LABELING_RUNTIME_CLASS_QUERY",
                ("DATABASE_URL_FILE", "CONTROL_TOKEN_FILE"),
            ),
            (
                "LABELING_RUNTIME_VIDEO_WRITER",
                ("DATABASE_URL_FILE", "CONTROL_TOKEN_FILE", "MASK_CONTENT_TOKEN_FILE"),
            ),
        )
        for suffix in suffixes
    )
    empty_selectors.update(
        f"{name}{suffix}"
        for name in (
            "CONNECTOR_ENCRYPTION_KEY",
            "ASSETS_SOURCE_TOKEN",
            "ASSETS_SOURCE_DATABASE_URL",
            "ASSETS_SOURCE_CONNECTIONS_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_DATABASE_URL",
            "ASSETS_SOURCE_SAVED_IO_READ_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_WRITE_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_CLEANUP_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_CUTOVER_TOKEN",
            "ASSETS_SOURCE_CLIENT_READ_TOKEN",
            "ASSETS_SOURCE_CLIENT_WRITE_TOKEN",
            "ASSETS_SOURCE_CLIENT_CLEANUP_TOKEN",
            "ASSETS_SOURCE_CLIENT_CONNECTIONS_TOKEN",
            "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY",
            "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN",
            "ASSETS_SOURCE_S3_ACCESS_KEY",
            "ASSETS_SOURCE_S3_SECRET_KEY",
            "ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN",
            "ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN",
            "ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN",
            "ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY",
            "ASSETS_SOURCE_NATIVE_S3_SECRET_KEY",
            "ASSETS_CELERY_VIDEO_DATABASE_URL",
            "ASSETS_CELERY_IMAGE_DATABASE_URL",
            "JOBS_DISPATCH_DATABASE_URL",
            "JOBS_PUBLICATION_DATABASE_URL",
            "NOTIFICATION_PUBLISH_REDIS_URL",
            "NOTIFICATION_SUBSCRIBE_REDIS_URL",
            "NOTIFICATION_PUBLICATION_TOKEN",
            "NOTIFICATION_PUBLISH_API_TOKEN",
            "NOTIFICATION_SOCKET_DATABASE_URL",
            "NOTIFICATION_SOCKET_REDIS_URL",
            "IDENTITY_MAIL_V2_SETTINGS_DATABASE_URL",
        )
        for suffix in ("", "_FILE")
    )
    empty_selectors.update(
        f"WORKSPACE_{operation}_{suffix}"
        for operation in (
            "MEMBERSHIP",
            "OWNER_TRANSFER",
            "CONFIGURATION",
            "TEMPLATE_QUERY",
            "PROJECT_QUERY",
            "DIRECTORY_QUERY",
        )
        for suffix in ("URL", "DATABASE_URL", "DATABASE_URL_FILE")
    )
    empty_selectors.update(
        f"{capability}_DATABASE_URL{suffix}"
        for capability in (
            "IDENTITY_AUTH",
            "IDENTITY_KEY",
            "IDENTITY_SESSION",
            "IDENTITY_ACCOUNT",
            "IDENTITY_GROUP",
            "IDENTITY_DIRECTORY",
            "IDENTITY_ADMIN_PASSWORD",
            "IDENTITY_PLATFORM_ROLE",
            "IDENTITY_SUSPENSION",
            "IDENTITY_DELETION",
            "IDENTITY_OFFBOARDING",
            "IDENTITY_BRIEFS",
            "IDENTITY_INVITATION",
            "IDENTITY_DEACTIVATION",
            "IDENTITY_DEACTIVATION_OPERATOR",
            "IDENTITY_PRESENCE",
            "IDENTITY_RECOVERY",
            "IDENTITY_MAIL",
            "IDENTITY_MAIL_V2",
            "IDENTITY_MAIL_SETTINGS",
            "NOTIFICATION_PUBLISH",
        )
        for suffix in ("", "_FILE")
    )
    return {
        **dict.fromkeys(empty_selectors, ""),
        "ASSETS_VIDEO_EXECUTOR": "celery",
        "ASSETS_IMAGE_EXECUTOR": "celery",
    }


def isolated_environment(
    resources: dict,
    *,
    code_root: Path,
    database_url: str,
    migration_database_url: str,
    minio_endpoint: str,
    redis_port: int,
    inherited: dict,
    minio_use_ssl: bool = False,
    require_inputs_bucket: bool = True,
) -> dict:
    """Return child-only overrides; never persist or print the connection strings."""
    if require_inputs_bucket and "MINIO_INPUTS_BUCKET" not in resources.get(
        "buckets", {}
    ):
        raise WorktreeError(
            "工作树尚未配置独立输入 bucket；请显式运行 init 完成资源升级"
        )

    runtime, migration = _validate_environment_sources(
        resources,
        database_url=database_url,
        migration_database_url=migration_database_url,
        minio_endpoint=minio_endpoint,
        redis_port=redis_port,
        inherited=inherited,
        minio_use_ssl=minio_use_ssl,
    )
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
        "YOAO_WORKTREE_RESOURCE_ROOT": resources["root"],
        "AAP_FIXTURE_TOOLS_ROOT": str(code_root / "scripts"),
        "YOAO_WORKTREE_MODE": resources["mode"],
        "FIXTURE_RUNTIME_DATABASE_URL": "",
        "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": redis_url,
        "CELERY_BROKER_READ_URL": redis_url,
        "CELERY_BROKER_WRITE_URL": redis_url,
        "CELERY_RESULT_BACKEND": redis_url,
        **resources["buckets"],
        **(
            {"ASSETS_MEDIA_INPUTS_BUCKET": resources["buckets"]["MINIO_INPUTS_BUCKET"]}
            if "MINIO_INPUTS_BUCKET" in resources["buckets"]
            else {}
        ),
        "MINIO_ENDPOINT": minio_endpoint,
        "MINIO_PUBLIC_URL": "/minio",
        "MINIO_PROXY_TARGET": f"{'https' if minio_use_ssl else 'http'}://{minio_endpoint}",
        "VITE_WS_HOST": "",
        "DUCKDB_PATH": str(directory / "data" / "analytics.duckdb"),
        "TMPDIR": str(directory / "tmp"),
        "TMP": str(directory / "tmp"),
        "TEMP": str(directory / "tmp"),
        "UV_PROJECT_ENVIRONMENT": str(code_root / "apps/api/.venv"),
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
        **isolated_module_environment(),
        "SMTP_HOST": "",
        "SENTRY_DSN": "",
    }
    if (
        resources["mode"] == "e2e"
        and inherited.get("PLAYWRIGHT_AI_REQUEST_WORKER") == "1"
    ):
        overrides.update(
            {
                "ASSETS_IMAGES_NATIVE_STORAGE_URL": "",
                "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN": "",
                "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN_FILE": "",
                "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL": "",
                "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN": "",
                "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN_FILE": "",
                "PREANNOTATION_RESULTS_DATABASE_URL": "",
                "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "",
            }
        )
    if resources["mode"] == "e2e":
        overrides.update(
            {
                f"INFERENCE_{owner}_{suffix}": ""
                for owner in (
                    "ROUTING",
                    "HEALTH_OBSERVATION",
                    "BACKEND_CONTROL",
                    "MODEL_EXECUTION",
                    "REGISTRY_INGRESS",
                    "GPU",
                )
                for suffix in (
                    "URL",
                    "TOKEN",
                    "TOKEN_FILE",
                    "DATABASE_URL",
                    "DATABASE_URL_FILE",
                )
            }
        )
        overrides.update(
            {
                f"AAP_MODULE_DATA_INFERENCE_{owner}_URL": ""
                for owner in ("ROUTING", "HEALTH_OBSERVATION")
            }
        )
        for key in inherited:
            upper = key.upper()
            if upper.startswith(
                (
                    "INFERENCE_BACKEND_ENDPOINT_",
                    "INFERENCE_GPU_",
                    "INFERENCE_HEALTH_DATA_",
                    "INFERENCE_BACKEND_STATS_",
                )
            ) and (
                upper.endswith("_DATABASE_URL") or upper.endswith("_DATABASE_URL_FILE")
            ):
                overrides[upper] = ""
    fixture_target_keys = (
        FIXTURE_ORIGINAL_ENV_KEYS if resources["mode"] in {"test", "e2e"} else ()
    )
    return {
        **{
            key: value
            for key, value in inherited.items()
            if key.upper() not in overrides and key.upper() not in fixture_target_keys
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
    """Remove privileged connections before launching ordinary processes."""

    def is_private_inference_dsn(key):
        upper = key.upper()
        return (
            upper.startswith("INFERENCE_")
            and upper.endswith(("_DATABASE_URL", "_DATABASE_URL_FILE"))
        ) or (
            upper.startswith("AAP_MODULE_DATA_INFERENCE_")
            and upper.endswith(("_URL", "_URL_FILE"))
        )

    return {
        **{
            key: value
            for key, value in environment.items()
            if not is_private_inference_dsn(key)
            and key.upper()
            not in {
                "JOBS_DISPATCH_DATABASE_URL",
                "JOBS_DISPATCH_DATABASE_URL_FILE",
                *_INSIGHTS_REFRESH_SELECTORS,
            }
        },
        **dict.fromkeys(_INSIGHTS_REFRESH_SELECTORS, ""),
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "PLAYWRIGHT_E2E_DATABASE_URL": "",
    }
