"""Runtime system settings registry, storage and effective-value resolution.

Deployment environment variables remain the baseline.  A row in
``system_settings`` is an explicit runtime override, except for historical rows
whose ``value_json`` is NULL: those rows intentionally keep the old inherit-env
semantics.  The service owns the editable-key registry so API validation and
business consumers use the same types and bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Literal

from sqlalchemy import delete, event, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models.system_setting import SystemSetting


ValueType = Literal["bool", "int", "str", "json"]
SettingSource = Literal["deployment", "override"]


@dataclass(frozen=True)
class SettingSpec:
    """One allow-listed setting and its public operational metadata."""

    key: str
    value_type: ValueType
    env_attr: str
    unit: str | None = None
    effect: str = ""
    min_value: int | None = None
    max_value: int | None = None
    dynamic_max: bool = False
    sensitive: bool = False

    @property
    def deployment_default(self) -> Any:
        return getattr(settings, self.env_attr, None)

    def bounds(self) -> tuple[int | None, int | None]:
        """Return bounds, widening only the documented deployment baseline."""

        if not self.dynamic_max:
            return self.min_value, self.max_value
        default = self.deployment_default
        if isinstance(default, bool) or not isinstance(default, int):
            default = 0
        code_default = type(settings).model_fields[self.env_attr].default
        upper = max(int(code_default), int(default))
        return self.min_value, upper


@dataclass(frozen=True)
class SettingsSnapshot:
    values: dict[str, Any]
    rows: dict[str, SystemSetting]
    version: str
    registration_default: bool | None = None


class SettingsVersionConflict(RuntimeError):
    """The caller wrote against a version superseded by another admin."""

    def __init__(self, expected: str, current: SettingsSnapshot):
        self.expected = expected
        self.current = current
        super().__init__(
            f"system settings version conflict: expected {expected}, "
            f"current {current.version}"
        )


SETTING_SPECS: dict[str, SettingSpec] = {
    "allow_open_registration": SettingSpec(
        "allow_open_registration",
        "bool",
        "allow_open_registration",
        effect="允许新用户自行注册为 viewer。",
    ),
    "invitation_ttl_days": SettingSpec(
        "invitation_ttl_days",
        "int",
        "invitation_ttl_days",
        unit="天",
        effect="控制新建邀请链接的有效期。",
        min_value=1,
        max_value=90,
    ),
    "frontend_base_url": SettingSpec(
        "frontend_base_url",
        "str",
        "frontend_base_url",
        effect="生成邀请、验证和密码重置链接时使用的前端地址。",
    ),
    "smtp_host": SettingSpec(
        "smtp_host", "str", "smtp_host", effect="SMTP 服务器主机名。"
    ),
    "smtp_port": SettingSpec(
        "smtp_port",
        "int",
        "smtp_port",
        effect="SMTP 服务器端口。",
        min_value=1,
        max_value=65_535,
    ),
    "smtp_user": SettingSpec(
        "smtp_user", "str", "smtp_user", effect="SMTP 登录用户名。"
    ),
    "smtp_password": SettingSpec(
        "smtp_password",
        "str",
        "smtp_password",
        effect="SMTP 登录密码；只返回是否已设置。",
        sensitive=True,
    ),
    "smtp_from": SettingSpec(
        "smtp_from", "str", "smtp_from", effect="SMTP 发件人地址。"
    ),
    "max_invitations_per_day": SettingSpec(
        "max_invitations_per_day",
        "int",
        "max_invitations_per_day",
        unit="次/滚动 24 小时",
        effect="按邀请人限制后续创建邀请的数量，滚动统计最近 24 小时；跨进程配置缓存最多约 30 秒。",
        min_value=1,
        max_value=1_000,
    ),
    "offline_threshold_minutes": SettingSpec(
        "offline_threshold_minutes",
        "int",
        "offline_threshold_minutes",
        unit="分钟",
        effect="仅影响在线状态显示，不退出会话；配置缓存最多约 30 秒，之后由每 2 分钟执行的下一轮扫描采用。",
        min_value=2,
        max_value=60,
    ),
    "dataset_import_max_files": SettingSpec(
        "dataset_import_max_files",
        "int",
        "dataset_import_max_files",
        unit="文件",
        effect="新导入受理时与字节预算一起固定；排队、运行及重试任务保持原预算，限制复制前冻结的来源文件数。",
        min_value=1,
        dynamic_max=True,
    ),
    "dataset_import_max_total_bytes": SettingSpec(
        "dataset_import_max_total_bytes",
        "int",
        "dataset_import_max_total_bytes",
        unit="bytes",
        effect="新导入受理时与文件数一起固定；约束连接器枚举和文件复制，不改变浏览器上传限制或作为全部网络流量限制。",
        min_value=1,
        dynamic_max=True,
    ),
    "task_create_sync_threshold": SettingSpec(
        "task_create_sync_threshold",
        "int",
        "task_create_sync_threshold",
        unit="条目",
        effect="数据集条目数不超过该值时在关联请求中同步建任务；0 表示非空数据集全部异步。",
        min_value=0,
        dynamic_max=True,
    ),
    "video_chunk_warmup_lookahead": SettingSpec(
        "video_chunk_warmup_lookahead",
        "int",
        "video_chunk_warmup_lookahead",
        unit="块",
        effect="视频主请求命中一个块后额外向后预热的块数；0 关闭额外预热。",
        min_value=0,
        dynamic_max=True,
    ),
    # The connector allowlist has a dedicated API, but is retained here for
    # compatibility with its existing service and DB/env fallback semantics.
    "connector_host_allowlist": SettingSpec(
        "connector_host_allowlist",
        "json",
        "connector_host_allowlist",
        effect="连接器目标主机白名单；由连接器专用接口管理。",
    ),
}

# Compatibility exports used by existing connector code and tests.
EDITABLE_KEYS: dict[str, str] = {
    key: spec.value_type for key, spec in SETTING_SPECS.items()
}
SENSITIVE_KEYS: set[str] = {
    key for key, spec in SETTING_SPECS.items() if spec.sensitive
}
SYSTEM_SETTINGS_METADATA_KEYS = tuple(
    key for key in SETTING_SPECS if key != "connector_host_allowlist"
)

_CACHE_TTL_SECONDS = 30
_cache: dict[str, tuple[float, Any]] = {}
_CHANGED_KEYS_INFO = "system_settings_changed_keys"
_SETTINGS_LOCK_NAME = "aap.system_settings"


def _env_default(key: str) -> Any:
    spec = SETTING_SPECS.get(key)
    if spec is None:
        return getattr(settings, key, None)
    return spec.deployment_default


def _deployment_default(key: str, registration_default: bool | None = None) -> Any:
    if key == "allow_open_registration" and registration_default is not None:
        return registration_default
    return _env_default(key)


def _coerce(value_type: str, raw: Any) -> Any:
    """Strictly decode JSON values; never turn ``"false"`` into ``True``."""

    if raw is None:
        return None
    if value_type == "bool":
        if type(raw) is not bool:
            raise TypeError("expected bool")
        return raw
    if value_type == "int":
        if type(raw) is not int:
            raise TypeError("expected int")
        return raw
    if value_type == "json":
        return raw
    if value_type == "str":
        if type(raw) is not str:
            raise TypeError("expected str")
        return raw
    raise TypeError(f"unknown setting type: {value_type}")


def _validate_stored_value(spec: SettingSpec, raw: Any) -> Any:
    if raw is None:
        # NULL is retained only for historical inherit-env rows.  The API does
        # not create such rows; reset is the explicit way to remove an override.
        return None
    value = _coerce(spec.value_type, raw)
    if spec.value_type == "int":
        low, high = spec.bounds()
        if low is not None and value < low:
            raise ValueError(f"{spec.key} must be >= {low}")
        if high is not None and value > high:
            raise ValueError(f"{spec.key} must be <= {high}")
    return value


def _cache_get(key: str) -> tuple[bool, Any]:
    rec = _cache.get(key)
    if rec is None:
        return False, None
    timestamp, value = rec
    if time.monotonic() - timestamp >= _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return False, None
    return True, value


def _cache_set(key: str, value: Any, read_started_at: float) -> None:
    # A slow read may finish after another process commits. Its TTL starts
    # before the query, so it cannot extend the propagation window.
    _cache[key] = (read_started_at, value)


def _version(
    rows: dict[str, SystemSetting], registration_default: bool | None = None
) -> str:
    """Stable opaque version that also changes when the last override is reset."""

    payload = []
    for key in sorted(rows):
        row = rows[key]
        payload.append(
            {
                "key": key,
                "value_type": row.value_type,
                "value_json": row.value_json,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "updated_by": str(row.updated_by)
                if row.updated_by is not None
                else None,
            }
        )
    encoded = json.dumps(
        {
            "overrides": payload,
            "deployment": {
                key: _deployment_default(key, registration_default)
                for key in SETTING_SPECS
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # Include deployment changes in concurrency checks without exposing an
    # unkeyed fingerprint that could be used to guess an SMTP password.
    digest = hmac.new(
        settings.secret_key.encode(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    return f"v1-{digest}"


def _mark_changed(db: AsyncSession, keys: set[str]) -> None:
    if keys:
        db.sync_session.info.setdefault(_CHANGED_KEYS_INFO, set()).update(keys)


@event.listens_for(Session, "after_commit")
def _invalidate_after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    keys = session.info.pop(_CHANGED_KEYS_INFO, set())
    if keys:
        SystemSettingsService.invalidate_many(keys)


@event.listens_for(Session, "after_rollback")
def _discard_pending_invalidation(session: Session) -> None:
    if session.in_nested_transaction():
        return
    keys = session.info.pop(_CHANGED_KEYS_INFO, set())
    SystemSettingsService.invalidate_many(keys)


class SystemSettingsService:
    """Read effective settings and serialize transactional overrides."""

    @staticmethod
    async def _lock(db: AsyncSession) -> None:
        """Serialize writes and expected-version checks until this tx commits."""

        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
            {"lock_name": _SETTINGS_LOCK_NAME},
        )

    @staticmethod
    def invalidate(key: str | None = None) -> None:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)

    @staticmethod
    def invalidate_many(keys: set[str] | list[str] | tuple[str, ...]) -> None:
        for key in keys:
            _cache.pop(key, None)

    @staticmethod
    async def _load_snapshot(
        db: AsyncSession,
        *,
        keys: set[str] | None = None,
        bypass_cache: bool = False,
        for_update: bool = False,
        registration_default: bool | None = None,
    ) -> SettingsSnapshot:
        if registration_default is not None and type(registration_default) is not bool:
            raise ValueError("registration_default must be a bool")
        selected_keys = keys or set(SETTING_SPECS)
        unknown = selected_keys.difference(SETTING_SPECS)
        if unknown:
            raise ValueError(f"非法配置项: {', '.join(sorted(unknown))}")

        read_started_at = time.monotonic()
        query = select(SystemSetting).execution_options(populate_existing=True)
        if selected_keys:
            query = query.where(SystemSetting.key.in_(selected_keys))
        if for_update:
            query = query.with_for_update()
        rows = {row.key: row for row in (await db.execute(query)).scalars().all()}

        values: dict[str, Any] = {}
        for key in selected_keys:
            spec = SETTING_SPECS[key]
            row = rows.get(key)
            deployment_default = _deployment_default(key, registration_default)
            value = deployment_default
            if row is not None and row.value_json is not None:
                try:
                    # Existing rows are decoded strictly but deliberately not
                    # range-validated: a deployment/override created before a
                    # narrower UI range must remain visible and be marked
                    # ``in_range=false`` in metadata.
                    value = _coerce(spec.value_type, row.value_json)
                except (TypeError, ValueError):
                    # Keep a malformed historical row from crashing every read;
                    # type-invalid data falls back to the deployment value.
                    value = deployment_default
            values[key] = value
            # A transaction can see its own uncommitted overrides. They must
            # never become a process-wide effective value, including after a
            # nested commit followed by an outer rollback.
            if (
                not bypass_cache
                and not db.sync_session.info.get(_CHANGED_KEYS_INFO)
                and not (
                    key == "allow_open_registration"
                    and registration_default is not None
                )
            ):
                _cache_set(key, value, read_started_at)
        return SettingsSnapshot(
            values=values,
            rows=rows,
            version=_version(rows, registration_default),
            registration_default=registration_default,
        )

    @staticmethod
    async def get(db: AsyncSession, key: str) -> Any:
        if key not in SETTING_SPECS:
            return _env_default(key)
        if not db.sync_session.info.get(_CHANGED_KEYS_INFO):
            hit, value = _cache_get(key)
            if hit:
                return value
        snapshot = await SystemSettingsService._load_snapshot(db, keys={key})
        return snapshot.values[key]

    @staticmethod
    async def get_many(
        db: AsyncSession,
        keys: list[str] | tuple[str, ...] | set[str],
        *,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        return (
            await SystemSettingsService._load_snapshot(
                db, keys=set(keys), bypass_cache=bypass_cache
            )
        ).values

    @staticmethod
    async def get_all(
        db: AsyncSession, *, bypass_cache: bool = False
    ) -> dict[str, Any]:
        return (
            await SystemSettingsService._load_snapshot(db, bypass_cache=bypass_cache)
        ).values

    @staticmethod
    async def snapshot(
        db: AsyncSession,
        *,
        bypass_cache: bool = False,
        registration_default: bool | None = None,
    ) -> SettingsSnapshot:
        return await SystemSettingsService._load_snapshot(
            db,
            bypass_cache=bypass_cache,
            registration_default=registration_default,
        )

    @staticmethod
    async def get_import_limits_snapshot(
        db: AsyncSession, *, registration_default: bool | None = None
    ) -> dict[str, Any]:
        snapshot = await SystemSettingsService._load_snapshot(
            db,
            bypass_cache=True,
            registration_default=registration_default,
        )
        return {
            "dataset_import_max_files": snapshot.values["dataset_import_max_files"],
            "dataset_import_max_total_bytes": snapshot.values[
                "dataset_import_max_total_bytes"
            ],
            "version": snapshot.version,
        }

    @staticmethod
    async def set_many(
        db: AsyncSession,
        updates: dict[str, Any],
        actor_id: Any | None,
        *,
        expected_version: str | None = None,
        registration_default: bool | None = None,
    ) -> dict[str, tuple[Any, Any]]:
        """Validate and write explicit overrides in the caller's transaction."""

        unknown = set(updates).difference(SETTING_SPECS)
        if unknown:
            raise ValueError(f"非法配置项: {', '.join(sorted(unknown))}")
        validated: dict[str, Any] = {}
        for key, raw in updates.items():
            if raw is None:
                continue
            try:
                validated[key] = _validate_stored_value(SETTING_SPECS[key], raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 类型或范围错误: {exc}") from exc
        await SystemSettingsService._lock(db)
        current = await SystemSettingsService._load_snapshot(
            db,
            keys=set(SETTING_SPECS),
            bypass_cache=True,
            for_update=True,
            registration_default=registration_default,
        )
        if expected_version is not None and expected_version != current.version:
            raise SettingsVersionConflict(expected_version, current)

        changes: dict[str, tuple[Any, Any]] = {}
        changed_keys: set[str] = set()
        for key, stored in validated.items():
            spec = SETTING_SPECS[key]
            old_value = current.values[key]
            row = current.rows.get(key)
            if row is not None and row.value_json is not None and old_value == stored:
                try:
                    _coerce(spec.value_type, row.value_json)
                except (TypeError, ValueError):
                    # A malformed historical override already falls back to
                    # this effective value; writing it still repairs the row.
                    pass
                else:
                    continue

            stmt = pg_insert(SystemSetting).values(
                key=key,
                value_type=spec.value_type,
                value_json=stored,
                updated_by=actor_id,
                updated_at=func.now(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SystemSetting.key],
                set_={
                    "value_json": stored,
                    "value_type": spec.value_type,
                    "updated_by": actor_id,
                    "updated_at": func.now(),
                },
            )
            await db.execute(stmt)
            changes[key] = (old_value, stored)
            changed_keys.add(key)
        _mark_changed(db, changed_keys)
        return changes

    @staticmethod
    async def reset_many(
        db: AsyncSession,
        keys: list[str] | tuple[str, ...] | set[str],
        actor_id: Any | None = None,
        *,
        expected_version: str | None = None,
        registration_default: bool | None = None,
    ) -> dict[str, tuple[Any, Any]]:
        selected = set(keys)
        if not selected:
            raise ValueError("至少指定一个配置项")
        unknown = selected.difference(SETTING_SPECS)
        if unknown:
            raise ValueError(f"非法配置项: {', '.join(sorted(unknown))}")
        await SystemSettingsService._lock(db)
        current = await SystemSettingsService._load_snapshot(
            db,
            keys=set(SETTING_SPECS),
            bypass_cache=True,
            for_update=True,
            registration_default=registration_default,
        )
        if expected_version is not None and expected_version != current.version:
            raise SettingsVersionConflict(expected_version, current)

        changes: dict[str, tuple[Any, Any]] = {}
        changed_keys: set[str] = set()
        for key in selected:
            row = current.rows.get(key)
            # Explicit reset also invalidates a potentially stale local cache
            # when another worker already removed the row.
            changed_keys.add(key)
            if row is None:
                continue
            old_value = current.values[key]
            await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
            changes[key] = (
                old_value,
                _deployment_default(key, registration_default),
            )
        _mark_changed(db, changed_keys)
        return changes

    @staticmethod
    async def reset(db: AsyncSession, key: str) -> None:
        """Compatibility wrapper for connector allowlist and older callers."""

        await SystemSettingsService.reset_many(db, [key])

    @staticmethod
    def metadata(snapshot: SettingsSnapshot) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key in SYSTEM_SETTINGS_METADATA_KEYS:
            spec = SETTING_SPECS[key]
            row = snapshot.rows.get(key)
            deployment_default = _deployment_default(key, snapshot.registration_default)
            if spec.sensitive:
                deployment_default = bool(deployment_default)
            source: SettingSource = (
                "override"
                if row is not None and row.value_json is not None
                else "deployment"
            )
            low, high = spec.bounds()
            effective = snapshot.values.get(key)
            in_range = True
            if spec.value_type == "int" and type(effective) is int:
                if low is not None and effective < low:
                    in_range = False
                if high is not None and effective > high:
                    in_range = False
            result[key] = {
                "source": source,
                "deployment_default": deployment_default,
                "updated_at": row.updated_at if row is not None else None,
                "updated_by": str(row.updated_by) if row and row.updated_by else None,
                "value_type": spec.value_type,
                "unit": spec.unit,
                "effect": spec.effect,
                "min_value": low,
                "max_value": high,
                "in_range": in_range,
            }
        return result

    @staticmethod
    def mask_for_response(key: str, value: Any) -> Any:
        if key in SENSITIVE_KEYS:
            return bool(value)
        return value

    @staticmethod
    def safe_audit_detail(changes: dict[str, tuple[Any, Any]]) -> dict[str, Any]:
        """Audit sensitive fields by change state only, never by value."""

        out: dict[str, Any] = {}
        for key, (old, new) in changes.items():
            if key in SENSITIVE_KEYS:
                out[key] = {"changed": old != new}
            else:
                out[key] = {"old": old, "new": new}
        return out
