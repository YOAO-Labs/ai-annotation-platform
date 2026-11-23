"""Runtime system settings registry, storage and effective-value resolution.

Deployment environment variables remain the baseline.  A row in
``system_settings`` is an explicit runtime override, except for historical rows
whose ``value_json`` is NULL: those rows intentionally keep the old inherit-env
semantics.  The service owns the editable-key registry so API validation and
business consumers use the same types and bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings


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
class SettingOverride:
    value_json: Any
    is_override: bool
    updated_at: datetime | None
    updated_by: str | None


@dataclass(frozen=True)
class SettingsSnapshot:
    values: dict[str, Any]
    rows: dict[str, SettingOverride]
    version: str
    stamp: str
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
    "connector_host_allowlist": SettingSpec(
        "connector_host_allowlist",
        "json",
        "connector_host_allowlist",
        effect="连接器目标主机白名单；由连接器专用接口管理。",
    ),
}

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
_KNOBS = {
    "offline_threshold_minutes",
    "task_create_sync_threshold",
    "video_chunk_warmup_lookahead",
}
_IMPORT_LIMITS = ("dataset_import_max_files", "dataset_import_max_total_bytes")

_RUNTIME_KNOB = text("SELECT yoao_settings_v1.runtime_knob(CAST(:key AS text))")
_CONNECTOR_OVERRIDE = text("SELECT yoao_settings_v1.connector_override()")
_IMPORT_SNAPSHOT = text("SELECT yoao_settings_v1.import_limits()")
_MANAGEMENT_SNAPSHOT = text(
    "SELECT yoao_settings_v1.management_snapshot(CAST(:actor AS uuid), CAST(:edit AS boolean))"
)
_APPLY_EDIT = text(
    "SELECT yoao_settings_v1.apply_settings_edit(CAST(:actor AS uuid), "
    "CAST(:stamp AS text), CAST(:updates AS jsonb), CAST(:resets AS text[]), "
    "CAST(:deployment_password AS text))"
)


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
    _cache[key] = (read_started_at, value)


def _version(stamp: str, registration_default: bool | None) -> str:
    encoded = json.dumps(
        {
            "overrides": stamp,
            "deployment": {
                key: _deployment_default(key, registration_default)
                for key in SETTING_SPECS
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hmac.new(
        settings.secret_key.encode(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    return f"v1-{digest}"


def _effective(key: str, raw: Any, registration_default: bool | None = None) -> Any:
    if raw is not None:
        try:
            return _coerce(SETTING_SPECS[key].value_type, raw)
        except (TypeError, ValueError):
            pass
    return _deployment_default(key, registration_default)


async def _flush(db: AsyncSession) -> None:
    if db.autoflush:
        await db.flush()


def _require_admin() -> HTTPException:
    return HTTPException(403, detail="需要角色权限: super_admin")


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
    async def snapshot(
        db: AsyncSession,
        actor_id: uuid.UUID,
        *,
        registration_default: bool | None = None,
        edit: bool = False,
    ) -> SettingsSnapshot:
        if registration_default is not None and type(registration_default) is not bool:
            raise ValueError("registration_default must be a bool")
        await _flush(db)
        raw = await db.scalar(_MANAGEMENT_SNAPSHOT, {"actor": actor_id, "edit": edit})
        if raw is None:
            raise _require_admin()
        rows = {
            key: SettingOverride(
                value_json=value["value"],
                is_override=value["is_override"],
                updated_at=datetime.fromisoformat(value["updated_at"])
                if value["updated_at"] is not None
                else None,
                updated_by=value["updated_by"],
            )
            for key, value in raw["overrides"].items()
        }
        values = {
            key: _effective(
                key,
                rows[key].value_json if key in rows else None,
                registration_default,
            )
            for key in SETTING_SPECS
            if key not in SENSITIVE_KEYS
        }
        password = rows.get("smtp_password")
        values["smtp_password"] = (
            password.value_json
            if password is not None and password.value_json is not None
            else bool(_env_default("smtp_password"))
        )
        return SettingsSnapshot(
            values=values,
            rows=rows,
            stamp=raw["stamp"],
            version=_version(raw["stamp"], registration_default),
            registration_default=registration_default,
        )

    @staticmethod
    async def connector_allowlist_state(db: AsyncSession) -> tuple[Any, bool]:
        await _flush(db)
        raw = await db.scalar(_CONNECTOR_OVERRIDE)
        return _effective("connector_host_allowlist", raw), raw is not None

    @staticmethod
    async def get(db: AsyncSession, key: str) -> Any:
        if key not in _KNOBS and key != "connector_host_allowlist":
            raise ValueError(f"非法配置项: {key}")
        cacheable = not db.sync_session.info.get(_CHANGED_KEYS_INFO)
        if cacheable:
            hit, value = _cache_get(key)
            if hit:
                return value
        read_started_at = time.monotonic()
        if key == "connector_host_allowlist":
            value, _ = await SystemSettingsService.connector_allowlist_state(db)
        else:
            await _flush(db)
            value = _effective(key, await db.scalar(_RUNTIME_KNOB, {"key": key}))
        if cacheable:
            _cache_set(key, value, read_started_at)
        return value

    @staticmethod
    async def get_import_limits_snapshot(
        db: AsyncSession, *, registration_default: bool | None = None
    ) -> dict[str, Any]:
        if registration_default is not None and type(registration_default) is not bool:
            raise ValueError("registration_default must be a bool")
        await _flush(db)
        raw = await db.scalar(_IMPORT_SNAPSHOT)
        return {
            **{
                key: _effective(key, raw["overrides"].get(key))
                for key in _IMPORT_LIMITS
            },
            "version": _version(raw["stamp"], registration_default),
        }

    @staticmethod
    async def _edit(
        db: AsyncSession,
        actor_id: uuid.UUID,
        *,
        updates: dict[str, Any],
        resets: set[str],
        expected_version: str | None,
        registration_default: bool | None,
    ) -> dict[str, dict[str, Any]]:
        current = await SystemSettingsService.snapshot(
            db, actor_id, registration_default=registration_default, edit=True
        )
        if expected_version is not None and expected_version != current.version:
            raise SettingsVersionConflict(expected_version, current)
        changed = await db.scalar(
            _APPLY_EDIT,
            {
                "actor": actor_id,
                "stamp": current.stamp,
                "updates": json.dumps(updates, allow_nan=False),
                "resets": sorted(resets),
                "deployment_password": (
                    _env_default("smtp_password")
                    if "smtp_password" in updates or "smtp_password" in resets
                    else None
                ),
            },
        )
        if changed is None:
            raise _require_admin()
        if changed.get("conflict"):
            refreshed = await SystemSettingsService.snapshot(
                db, actor_id, registration_default=registration_default, edit=True
            )
            raise SettingsVersionConflict(
                expected_version or current.version, refreshed
            )
        changes = changed["changes"]
        _mark_changed(db, resets | changes.keys())
        return {
            key: (
                {"changed": value}
                if key in SENSITIVE_KEYS
                else {
                    "old": current.values[key],
                    "new": (
                        updates[key]
                        if key in updates
                        else _deployment_default(key, registration_default)
                    ),
                }
            )
            for key, value in changes.items()
        }

    @staticmethod
    async def set_many(
        db: AsyncSession,
        updates: dict[str, Any],
        actor_id: uuid.UUID,
        *,
        expected_version: str | None = None,
        registration_default: bool | None = None,
    ) -> dict[str, dict[str, Any]]:
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
        return await SystemSettingsService._edit(
            db,
            actor_id,
            updates=validated,
            resets=set(),
            expected_version=expected_version,
            registration_default=registration_default,
        )

    @staticmethod
    async def reset_many(
        db: AsyncSession,
        keys: list[str] | tuple[str, ...] | set[str],
        actor_id: uuid.UUID,
        *,
        expected_version: str | None = None,
        registration_default: bool | None = None,
    ) -> dict[str, dict[str, Any]]:
        selected = set(keys)
        if not selected:
            raise ValueError("至少指定一个配置项")
        unknown = selected.difference(SETTING_SPECS)
        if unknown:
            raise ValueError(f"非法配置项: {', '.join(sorted(unknown))}")
        return await SystemSettingsService._edit(
            db,
            actor_id,
            updates={},
            resets=selected,
            expected_version=expected_version,
            registration_default=registration_default,
        )

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
                "override" if row is not None and row.is_override else "deployment"
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
                "updated_by": row.updated_by if row is not None else None,
                "value_type": spec.value_type,
                "unit": spec.unit,
                "effect": spec.effect,
                "min_value": low,
                "max_value": high,
                "in_range": in_range,
            }
        return result
