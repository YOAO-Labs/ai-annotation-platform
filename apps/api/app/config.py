import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode
from sqlalchemy.engine import make_url

from yoao_inference.gpu.settings import (
    GPUArbiterMode,
    GPUResourceSettings,
    parse_gpu_resources,
)

_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT_ENV = (
    _PARENTS[3] / ".env" if len(_PARENTS) > 3 else Path("/nonexistent/.env")
)
_REPO_ROOT_DUCKDB = (
    str(_PARENTS[3] / "data" / "duckdb" / "analytics.duckdb")
    if len(_PARENTS) > 3
    else "./data/duckdb/analytics.duckdb"
)


def _default_duckdb_path(data: dict) -> str:
    """Keep API and offline seed analytics on the same isolated test store."""
    database_url = make_url(data["database_url"])
    database = database_url.database or ""
    if database_url.get_backend_name() == "postgresql" and database.endswith(
        ("_test", "_e2e")
    ):
        identity = json.dumps([database_url.host, database_url.port, database])
        store_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
        root = _PARENTS[3] if len(_PARENTS) > 3 else Path.cwd()
        return str(root / ".artifacts" / "analytics" / f"{store_id}.duckdb")
    return _REPO_ROOT_DUCKDB


_GPU_ARBITER_MODE_ORDER = {
    GPUArbiterMode.OFF: 0,
    GPUArbiterMode.OBSERVE: 1,
    GPUArbiterMode.ENFORCE: 2,
}


class SftpReferenceRoot(BaseModel):
    """Deployment attestation: this account's entire namespace is a read-only jail."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9_.:-]+$")
    port: int = Field(default=22, strict=True, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    host_key: str = Field(min_length=20, max_length=4096)

    @field_validator("host_key")
    @classmethod
    def _validate_host_key(cls, value):
        import paramiko

        try:
            entry = paramiko.hostkeys.HostKeyEntry.from_line("qualified " + value)
            if entry is None or entry.key is None or len(value.split()) != 2:
                raise ValueError()
        except Exception:
            raise ValueError("Invalid pinned SFTP public host key") from None
        return value


class Settings(BaseSettings):
    app_name: str = "YOAO API"
    app_version: str = "0.26.2"
    debug: bool = True
    environment: Literal["development", "staging", "production"] = "development"
    e2e_seed_enabled: bool = False

    database_url: str = "postgresql+asyncpg://user:pass@localhost:5432/annotation"
    migration_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    cors_allow_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5173",
    ]
    cors_allow_origin_regex: str | None = r"http://localhost:\d+"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        """允许 env 用 JSON list 或逗号分隔字符串。"""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @property
    def effective_cors_origin_regex(self) -> str | None:
        """production 强制不放 regex，避免误用本机正则上线。"""
        if self.environment == "production":
            return None
        return self.cors_allow_origin_regex

    secret_key: str = "dev-secret-change-in-production"
    access_token_expire_minutes: int = 60 * 24

    connector_encryption_key: str = ""
    connector_host_allowlist: Annotated[list[str], NoDecode] = []
    connector_deployment_sftp_host: str = ""
    storage_maintenance_protocol_enabled: bool = False
    storage_filesystem_roots: dict[str, str] = Field(default_factory=dict)
    storage_sftp_roots: dict[str, SftpReferenceRoot] = Field(default_factory=dict)

    @field_validator("storage_sftp_roots")
    @classmethod
    def _validate_sftp_roots(cls, value):
        import re

        targets = [(root.host, root.port, root.username) for root in value.values()]
        if len(targets) != len(set(targets)) or any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", handle) for handle in value
        ):
            raise ValueError("Invalid or ambiguous SFTP reference roots")
        return value

    @field_validator("storage_filesystem_roots")
    @classmethod
    def _validate_filesystem_roots(cls, value):
        import re

        for handle, path in value.items():
            if (
                not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", handle)
                or not path.startswith("/")
                or path == "/"
                or "\x00" in path
                or any(part in {".", "..", ""} for part in path[1:].split("/"))
            ):
                raise ValueError("Invalid controlled storage root")
        return value

    @field_validator("connector_host_allowlist", mode="before")
    @classmethod
    def _parse_connector_host_allowlist(cls, v):
        """允许 env 用 JSON list 或逗号分隔字符串。"""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [entry.strip() for entry in v.split(",") if entry.strip()]
        return v

    dataset_import_max_files: int = 50_000
    dataset_import_max_total_bytes: int = 200 * 1024 * 1024 * 1024

    mask_format_temp_quota_bytes: int = 8 * 1024 * 1024 * 1024
    mask_format_max_entry_bytes: int = 512 * 1024 * 1024
    mask_format_max_archive_files: int = 200_000
    mask_format_max_compression_ratio: float = 100.0

    task_create_sync_threshold: int = 2000

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "annotations"
    minio_datasets_bucket: str = "datasets"
    minio_bug_reports_bucket: str = "bug-reports"
    minio_media_cache_bucket: str = "media-cache"
    minio_audit_archive_bucket: str = "audit-archive"
    audit_archive_read_s3_endpoint_url: str = ""
    audit_archive_read_s3_region: str = "us-east-1"
    audit_archive_read_s3_bucket: str = "audit-archive"
    audit_archive_read_s3_access_key_id: str = Field(default="", repr=False)
    audit_archive_read_s3_access_key_id_file: str = ""
    audit_archive_read_s3_secret_access_key: str = Field(default="", repr=False)
    audit_archive_read_s3_secret_access_key_file: str = ""
    minio_import_bucket: str = "import"
    minio_export_bucket: str = "export"
    minio_avatars_bucket: str = "avatars"
    minio_training_bucket: str = "training"
    minio_inputs_bucket: str = "accepted-inputs"
    training_enabled: bool = False
    training_executor_token: str = Field(default="", repr=False)
    training_node_id: str = "local"
    training_gpu_uuid: str = ""
    training_catalog_json: dict[str, dict] = Field(default_factory=dict)
    training_heartbeat_timeout_seconds: int = Field(default=60, ge=30, le=600)
    minio_use_ssl: bool = False
    minio_public_url: str = ""

    identity_url: str = ""
    identity_token: str = Field(default="", repr=False)
    identity_timeout_seconds: float = Field(default=20, gt=0, le=120)
    identity_auth_database_url: str = Field(default="", repr=False)
    identity_auth_database_url_file: str = ""
    identity_key_database_url: str = Field(default="", repr=False)
    identity_key_database_url_file: str = ""
    identity_session_database_url: str = Field(default="", repr=False)
    identity_session_database_url_file: str = ""
    identity_account_database_url: str = Field(default="", repr=False)
    identity_account_database_url_file: str = ""
    identity_group_database_url: str = Field(default="", repr=False)
    identity_group_database_url_file: str = ""
    identity_directory_database_url: str = Field(default="", repr=False)
    identity_directory_database_url_file: str = ""
    identity_admin_password_database_url: str = Field(default="", repr=False)
    identity_admin_password_database_url_file: str = ""
    identity_platform_role_database_url: str = Field(default="", repr=False)
    identity_platform_role_database_url_file: str = ""
    identity_suspension_database_url: str = Field(default="", repr=False)
    identity_suspension_database_url_file: str = ""
    identity_deletion_database_url: str = Field(default="", repr=False)
    identity_deletion_database_url_file: str = ""
    identity_offboarding_database_url: str = Field(default="", repr=False)
    identity_offboarding_database_url_file: str = ""
    identity_briefs_database_url: str = Field(default="", repr=False)
    identity_briefs_database_url_file: str = ""
    identity_invitation_database_url: str = Field(default="", repr=False)
    identity_invitation_database_url_file: str = ""
    identity_invitation_mail_timeout_seconds: float = Field(default=60, gt=0, le=120)
    identity_deactivation_database_url: str = Field(default="", repr=False)
    identity_deactivation_database_url_file: str = ""
    identity_deactivation_operator_database_url: str = Field(default="", repr=False)
    identity_deactivation_operator_database_url_file: str = ""
    notification_socket_url: str = ""
    notification_publication_url: str = ""
    notification_publication_token: str = Field(default="", repr=False)
    notification_publication_token_file: str = ""
    jobs_queue_url: str = ""
    jobs_queue_token: str = Field(default="", repr=False)
    jobs_queue_token_file: str = ""
    notification_publish_database_url: str = Field(default="", repr=False)
    notification_publish_database_url_file: str = ""
    identity_presence_database_url: str = Field(default="", repr=False)
    identity_presence_database_url_file: str = ""
    identity_recovery_database_url: str = Field(default="", repr=False)
    identity_recovery_database_url_file: str = ""
    identity_recovery_mail_key: str = Field(default="", repr=False)
    identity_recovery_mail_key_file: str = ""
    identity_mail_database_url: str = Field(default="", repr=False)
    identity_mail_database_url_file: str = ""
    identity_mail_v2_database_url: str = Field(default="", repr=False)
    identity_mail_v2_database_url_file: str = ""
    identity_mail_settings_database_url: str = Field(default="", repr=False)
    identity_mail_settings_database_url_file: str = ""

    workspace_membership_timeout_seconds: float = Field(default=30, gt=0, le=3600)

    labeling_class_query_url: str = ""
    labeling_class_query_database_url: str = Field(default="", repr=False)
    labeling_class_query_database_url_file: str = ""
    labeling_class_query_control_token: str = Field(default="", repr=False)
    labeling_class_query_control_token_file: str = ""
    labeling_class_query_timeout_seconds: float = Field(default=30, gt=0, le=3600)

    labeling_video_writer_url: str = ""
    labeling_video_writer_database_url: str = Field(default="", repr=False)
    labeling_video_writer_database_url_file: str = ""
    labeling_video_writer_control_token: str = Field(default="", repr=False)
    labeling_video_writer_control_token_file: str = ""
    labeling_video_writer_timeout_seconds: float = Field(default=90, gt=0, le=3600)

    inference_pipelines_url: str = ""
    inference_pipelines_token: str = Field(default="", repr=False)
    inference_pipelines_token_file: str = ""
    inference_pipelines_timeout_seconds: float = Field(default=30, gt=0, le=3600)

    inference_backend_probe_url: str = Field(default="", repr=False)
    inference_backend_probe_token: str = Field(default="", repr=False)
    inference_backend_probe_token_file: str = ""
    inference_backend_probe_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    inference_health_observation_url: str = Field(default="", repr=False)
    inference_health_observation_token: str = Field(default="", repr=False)
    inference_health_observation_token_file: str = ""
    inference_health_observation_timeout_seconds: float = Field(
        default=90, gt=0, le=3600
    )
    inference_registry_ingress_url: str = Field(default="", repr=False)
    inference_registry_ingress_token: str = Field(default="", repr=False)
    inference_registry_ingress_token_file: str = ""
    inference_registry_ingress_timeout_seconds: float = Field(default=90, gt=0, le=3600)
    inference_routing_url: str = Field(default="", repr=False)
    inference_routing_token: str = Field(default="", repr=False)
    inference_routing_token_file: str = ""
    inference_routing_timeout_seconds: float = Field(default=30, gt=0, le=3600)

    inference_model_execution_url: str = Field(default="", repr=False)
    inference_model_execution_token: str = Field(default="", repr=False)
    inference_model_execution_token_file: str = ""
    inference_backend_control_url: str = Field(default="", repr=False)
    inference_backend_control_token: str = Field(default="", repr=False)
    inference_backend_control_token_file: str = ""
    inference_gpu_url: str = Field(default="", repr=False)
    inference_gpu_token: str = Field(default="", repr=False)
    inference_gpu_token_file: str = ""

    media_probe_url: str = ""
    media_probe_token: str = Field(default="", repr=False)
    media_probe_timeout_seconds: float = Field(default=90, gt=0, le=3600)

    assets_catalog_url: str = ""
    assets_catalog_token: str = Field(default="", repr=False)
    assets_catalog_timeout_seconds: float = Field(default=5, gt=0, le=60)

    assets_source_url: str = ""
    assets_source_token: str = Field(default="", repr=False)
    assets_source_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    assets_media_url: str = ""
    assets_media_token: str = Field(default="", repr=False)
    assets_media_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    assets_images_native_storage_url: str = ""
    assets_images_native_storage_token: str = Field(default="", repr=False)
    assets_images_native_storage_token_file: str = ""
    assets_media_native_image_storage_url: str = ""
    assets_media_native_image_storage_token: str = Field(default="", repr=False)
    assets_media_native_image_storage_token_file: str = ""

    assets_mask_content_url: str = ""
    assets_mask_content_token: str = Field(default="", repr=False)
    assets_mask_cleanup_url: str = ""
    assets_mask_cleanup_token: str = Field(default="", repr=False)
    assets_mask_content_timeout_seconds: float = Field(default=30, gt=0, le=300)
    labeling_mask_gc_url: str = ""
    labeling_mask_gc_token: str = Field(default="", repr=False)
    labeling_mask_gc_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    preannotation_results_database_url: str = Field(default="", repr=False)
    preannotation_results_database_url_file: str = ""
    jobs_publication_database_url: str = Field(default="", repr=False)
    jobs_publication_database_url_file: str = ""
    export_execution_database_url: str = Field(default="", repr=False)
    export_execution_database_url_file: str = ""

    image_pyramid_auto_generate: bool = False
    image_pyramid_profile_version: str = "pyramid-v1"
    image_pyramid_normalization_version: str = "exif-autorotate-srgb-v1"
    image_pyramid_optional_pixels: int = 16_777_216
    image_pyramid_required_pixels: int = 50_000_000
    image_pyramid_max_pixels: int = 300_000_000
    image_pyramid_max_dimension: int = 32_768
    image_pyramid_max_tiles: int = 20_000
    image_pyramid_max_source_bytes: int = 8 * 1024 * 1024 * 1024
    image_pyramid_max_derived_bytes: int = 8 * 1024 * 1024 * 1024
    image_pyramid_max_temp_bytes: int = 12 * 1024 * 1024 * 1024
    image_pyramid_job_timeout_seconds: int = Field(default=1_800, gt=0)
    image_pyramid_lease_seconds: int = Field(default=2_100, gt=0)

    @model_validator(mode="after")
    def pyramid_lease_outlasts_writer(self):
        if (
            self.image_pyramid_lease_seconds
            <= self.image_pyramid_job_timeout_seconds + 60
        ):
            raise ValueError(
                "Image pyramid lease must outlast the worker hard time limit"
            )
        return self

    @model_validator(mode="after")
    def accepted_inputs_bucket_is_independent(self):
        buckets = {
            self.minio_bucket,
            self.minio_datasets_bucket,
            self.minio_bug_reports_bucket,
            self.minio_media_cache_bucket,
            self.minio_audit_archive_bucket,
            self.minio_import_bucket,
            self.minio_export_bucket,
            self.minio_avatars_bucket,
            self.minio_training_bucket,
        }
        if self.minio_inputs_bucket in buckets:
            raise ValueError("MINIO_INPUTS_BUCKET must name a dedicated bucket")
        return self

    image_pyramid_url_expiry_seconds: int = 900
    image_pyramid_asset_url_batch_max: int = 128
    image_pyramid_retry_cooldown_seconds: int = 300
    image_pyramid_gc_grace_hours: int = 24
    image_pyramid_vips_concurrency: int = 4
    image_pyramid_srgb_profile: str = "/usr/share/color/icc/sRGB.icc"

    ml_backend_storage_host: str = ""

    ml_managed_backend_urls: Annotated[dict[str, str], NoDecode] = {
        "yolo": "http://172.17.0.1:8003",
        "onnxtools": "http://172.17.0.1:8004",
        "rapidocr": "http://172.17.0.1:8005",
    }
    ml_managed_auth_dir: str = str(
        _PARENTS[3] / "data" / "inference-auth"
        if len(_PARENTS) > 3
        else Path("/run/aap-inference-auth")
    )
    ml_backend_operations_dir: str = str(
        _PARENTS[3] / "data" / "backend-manager"
        if len(_PARENTS) > 3
        else Path("/run/aap-backend-manager")
    )

    @field_validator("ml_managed_auth_dir", "ml_backend_operations_dir")
    @classmethod
    def _deployment_local_directory(cls, value: str) -> str:
        path = Path(value)
        root = _PARENTS[3] if len(_PARENTS) > 3 else Path.cwd()
        return str(path if path.is_absolute() else root / path)

    @field_validator("ml_managed_backend_urls", mode="before")
    @classmethod
    def _managed_backend_urls(cls, value):
        from yoao_protocol_v2.backend_auth import validate_backend_urls

        if isinstance(value, str) and not value.strip():
            value = cls.model_fields["ml_managed_backend_urls"].default
        parsed = json.loads(value) if isinstance(value, str) else value
        return validate_backend_urls(parsed)

    ml_backend_default_url: str = ""

    ml_backend_observe_urls: Annotated[list[str], NoDecode] = []

    @field_validator("ml_backend_observe_urls", mode="before")
    @classmethod
    def _parse_observe_urls(cls, v):
        """允许 env 用 JSON list 或逗号分隔字符串。"""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [u.strip() for u in v.split(",") if u.strip()]
        return v

    gpu_arbiter_mode: GPUArbiterMode = GPUArbiterMode.OFF
    gpu_arbiter_rollout_enabled: bool = False
    gpu_arbiter_resources_json: Annotated[str, NoDecode] = "{}"
    gpu_arbiter_admission_timeout_seconds: int = Field(default=30, gt=0, le=3600)

    ml_backend_router_mode: Literal["off", "observe", "enforce"] = "off"
    ml_backend_router_health_max_age_seconds: int = Field(default=90, gt=0, le=3600)
    ml_backend_router_lease_ttl_seconds: int = Field(default=120, gt=0, le=3600)
    ml_backend_router_heartbeat_interval_seconds: int = Field(default=15, gt=0, le=600)
    ml_backend_router_passive_failure_threshold: int = Field(default=3, gt=0, le=100)
    ml_backend_router_eject_seconds: int = Field(default=30, gt=0, le=3600)
    raster_mask_read_enabled: bool = True
    raster_mask_create_enabled: bool = True
    gpu_arbiter_residency_cooldown_seconds: int = Field(default=30, gt=0, le=3600)

    def _parse_gpu_arbiter_resources(
        self,
    ) -> tuple[dict[str, GPUResourceSettings], tuple[str, ...]]:
        return parse_gpu_resources(self.gpu_arbiter_resources_json)

    @property
    def gpu_arbiter_resources(self) -> dict[str, GPUResourceSettings]:
        return self._parse_gpu_arbiter_resources()[0]

    @property
    def gpu_arbiter_config_errors(self) -> tuple[str, ...]:
        return self._parse_gpu_arbiter_resources()[1]

    def gpu_arbiter_desired_mode(self, resource_id: str) -> GPUArbiterMode:
        """Return desired mode after applying the global ceiling; unknown is off."""

        resource = self.gpu_arbiter_resources.get(resource_id)
        if resource is None:
            return GPUArbiterMode.OFF
        resource_mode = resource.mode or GPUArbiterMode.OFF
        return min(
            (self.gpu_arbiter_mode, resource_mode),
            key=_GPU_ARBITER_MODE_ORDER.__getitem__,
        )

    ml_predict_timeout: int = 100
    ml_health_timeout: int = 10

    metrics_sd_token: str = ""

    celery_broker_url: str = ""

    preannotate_gpu_queue: str = "ml"
    preannotate_cpu_queue: str = "ml.cpu"

    video_chunk_size_frames: int = 60
    video_chunk_warmup_lookahead: int = 1
    video_frame_cache_ttl_days: int = 14
    video_chunk_cache_ttl_days: int = 30
    video_frame_memory_cache_items: int = 64
    video_segment_size_frames: int = 18000
    video_segment_lock_ttl_seconds: int = 300
    video_tracker_window_size_frames: int = 300
    video_tracker_sam3_window_size_frames: int = 16
    video_tracker_low_confidence_outside_threshold: float = 0.15
    tracker_soft_time_limit_seconds: int = 1800

    frame_preannotate_max_frames: int = 900
    frame_preannotate_chunk_size: int = 30
    frame_preannotate_max_boxes_per_frame: int = 100
    frame_preannotate_segment_soft_time_limit_seconds: int = 900

    audit_async: bool = True

    audit_retention_months: int = 12

    task_events_async: bool = True

    duckdb_path: str = Field(default_factory=_default_duckdb_path)

    frontend_base_url: str = "http://localhost:5173"
    invitation_ttl_days: int = 7
    allow_open_registration: bool = False
    max_invitations_per_day: int = 30

    require_email_verification: bool | None = None

    cors_allow_methods: list[str] = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"]
    cors_allow_headers: list[str] = [
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "Idempotency-Key",
    ]

    offline_threshold_minutes: int = 5

    worker_heartbeat_interval_seconds: int = 30

    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password_file: str = Field(default="", repr=False, exclude=True)
    smtp_password: str | None = Field(default=None, repr=False)
    smtp_from: str | None = None

    @field_validator("smtp_password")
    @classmethod
    def _load_smtp_password_file(
        cls, password: str | None, info: ValidationInfo
    ) -> str | None:
        path = info.data.get("smtp_password_file")
        if not path:
            return password
        if password:
            raise RuntimeError("SMTP_PASSWORD 和 SMTP_PASSWORD_FILE 只能设置一项")
        try:
            password = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise RuntimeError("无法读取 SMTP_PASSWORD_FILE") from None
        if not password:
            raise RuntimeError("SMTP_PASSWORD_FILE 不能为空")
        return password

    sentry_dsn: str | None = None
    sentry_environment: str = "development"
    sentry_traces_sample_rate: float = 0.1

    turnstile_enabled: bool = False
    turnstile_site_key: str | None = None
    turnstile_secret_key: str | None = None
    turnstile_verify_url: str = (
        "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    )

    login_captcha_threshold: int = 5
    login_failed_window_seconds: int = 3600

    @property
    def effective_celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def effective_migration_database_url(self) -> str:
        return self.migration_database_url or self.database_url

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_port and self.smtp_from)

    @property
    def email_verification_required(self) -> bool:
        """开放注册是否强制邮箱验证。None → production 开、其它环境关。"""
        if self.require_email_verification is not None:
            return self.require_email_verification
        return self.environment == "production"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        for operation in (
            "membership",
            "owner_transfer",
            "configuration",
            "template_query",
            "project_query",
            "directory_query",
        ):
            for suffix in ("url", "database_url", "database_url_file"):
                key = f"workspace_{operation}_{suffix}"
                if env_settings.env_vars.get(key, dotenv_settings.env_vars.get(key)):
                    raise RuntimeError(
                        f"{key.upper()} selects a retired Workspace service; "
                        "migrate the deployment before starting the embedded API"
                    )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    class Config:
        env_file = str(_REPO_ROOT_ENV)
        extra = "ignore"


settings = Settings()
