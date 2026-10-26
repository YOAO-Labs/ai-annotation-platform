"""Collect and verify non-production GPU arbitration acceptance evidence.

The runner never migrates the database, changes rollout mode, repairs Redis, or
starts/stops services. ``preflight`` is strictly read-only. ``run`` enters the
real dispatch authority directly because production effective enforce remains
closed until P6; it requires an exact run-id confirmation and refuses production.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
from statistics import median
import time
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    BackendResidency,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    managed_lifecycle_capability_sha256,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import or_, select, text

from app.config import Settings, settings
from app.db.models.gpu_backend_cancel_intent import GPUBackendCancelIntent
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.backend_transport import BackendTransport
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUDispatchRequest
from aap_inference.gpu.ledger import GPUArbiterStore, GPUArbiterStoreError
from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_workspace.configuration import secret_source
from app.observability.metrics import observe_ml_backend
from app.utils.gpu_resource import validate_gpu_resource_id


def _owner_database_url(name: str) -> str:
    value = secret_source(os.environ.get(name, ""), os.environ.get(f"{name}_FILE", ""))
    if not value:
        raise ValueError(f"{name} or {name}_FILE is required for owner validation")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _owner_settings(config: Settings) -> GPUAuthoritySettings:
    return GPUAuthoritySettings(
        gpu_arbiter_mode=config.gpu_arbiter_mode,
        gpu_arbiter_resources=config.gpu_arbiter_resources,
        gpu_arbiter_config_errors=tuple(config.gpu_arbiter_config_errors),
        gpu_arbiter_rollout_enabled=config.gpu_arbiter_rollout_enabled,
        gpu_arbiter_admission_timeout_seconds=config.gpu_arbiter_admission_timeout_seconds,
        gpu_arbiter_residency_cooldown_seconds=config.gpu_arbiter_residency_cooldown_seconds,
        ml_predict_timeout=config.ml_predict_timeout,
        redis_url=config.redis_url,
        gpu_lifecycle_signing_keys_file=os.environ.get(
            "GPU_LIFECYCLE_SIGNING_KEYS_FILE", ""
        ),
        gpu_lifecycle_active_signing_kid=os.environ.get(
            "GPU_LIFECYCLE_ACTIVE_SIGNING_KID", ""
        ),
        gpu_arbiter_collector_database_url_file=os.environ.get(
            "GPU_ARBITER_COLLECTOR_DATABASE_URL_FILE", ""
        ),
    )


@asynccontextmanager
async def _readonly_dispatch(_operation, _scope):
    raise RuntimeError("Backend observation cannot issue a GPU mutation")
    yield  # The context manager must reject entry rather than provide a grant.


EVIDENCE_SCHEMA = "aap.gpu-arbitration.acceptance/v1"
SAMPLE_COUNT = 5
SAMPLE_INTERVAL_SECONDS = 0.5
STABLE_MEMORY_SPREAD_MB = 64
MIN_MEMORY_RECOVERY_RATIO = 0.90
MIN_PARALLEL_OVERLAP_MS = 500
PREFLIGHT_SNAPSHOT_ATTEMPTS = 3
NVIDIA_SMI_TIMEOUT_SECONDS = 10
SUBPROCESS_TERMINATE_TIMEOUT_SECONDS = 1
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_GPU_UUID_RE = re.compile(r"GPU-[A-Za-z0-9][A-Za-z0-9-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GPU_HEALTH_CHALLENGE_ECHO_MARKER = "_aap_gpu_health_challenge_echo"
_ACTION_SCOPE = {
    "predict": AdmissionScope.PREDICT,
    "predict_interactive": AdmissionScope.PREDICT,
    "warmup": AdmissionScope.WARMUP,
    "reload": AdmissionScope.RELOAD,
}
_ACTION_PATH = {
    "predict": "/predict",
    "predict_interactive": "/predict",
    "warmup": "/warmup",
    "reload": "/reload",
}
_CURRENT_ACTION_ID: ContextVar[str | None] = ContextVar(
    "gpu_arbitration_validation_action_id", default=None
)
_PRIVATE_FINGERPRINT_KEY = secrets.token_bytes(32)


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    gpu_uuid: str

    @model_validator(mode="after")
    def validate_identity(self) -> "ResourceSpec":
        validate_gpu_resource_id(self.resource_id)
        if _GPU_UUID_RE.fullmatch(self.gpu_uuid) is None:
            raise ValueError("gpu_uuid must be a canonical NVIDIA GPU UUID")
        return self


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    role: Literal["requester", "victim", "peer"]
    backend_id: str
    resource_id: str
    operation: Literal["predict", "predict_interactive", "warmup", "reload"]
    body: dict[str, Any] = Field(default_factory=dict)
    expected_error_code: Literal["gpu_capacity_unavailable"] | None = None
    delay_ms: int = Field(default=0, ge=0, le=3_600_000)
    timeout_seconds: float = Field(default=600, gt=0, le=3_600)

    @model_validator(mode="after")
    def validate_action(self) -> "ActionSpec":
        if _RUN_ID_RE.fullmatch(self.id) is None:
            raise ValueError("action id must match [A-Za-z0-9._-]{1,128}")
        try:
            normalized_backend_id = str(uuid.UUID(self.backend_id))
        except ValueError as exc:
            raise ValueError("backend_id must be a canonical UUID") from exc
        if normalized_backend_id != self.backend_id:
            raise ValueError("backend_id must be a canonical UUID")
        validate_gpu_resource_id(self.resource_id)
        if self.operation == "predict":
            if not isinstance(self.body.get("tasks"), list) or set(self.body) - {
                "tasks",
                "context",
            }:
                raise ValueError("predict body must contain tasks and optional context")
        elif self.operation == "predict_interactive" and set(self.body) != {
            "task",
            "context",
        }:
            raise ValueError(
                "predict_interactive body must contain exactly task and context"
            )
        return self


class ValidationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    cohort_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=255)
    scenario: Literal[
        "single-card-co-residency",
        "single-card-eviction",
        "single-card-capacity-rejection",
        "dual-card",
        "cross-host",
    ]
    resources: list[ResourceSpec] = Field(min_length=1)
    actions: list[ActionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_topology(self) -> "ValidationManifest":
        if _RUN_ID_RE.fullmatch(self.cohort_id) is None:
            raise ValueError("cohort_id must match [A-Za-z0-9._-]{1,128}")
        if (
            self.node_id != self.node_id.strip()
            or any(ch.isspace() for ch in self.node_id)
            or "/" in self.node_id
        ):
            raise ValueError("node_id must be canonical and must not contain /")
        resource_ids = [resource.resource_id for resource in self.resources]
        gpu_uuids = [resource.gpu_uuid for resource in self.resources]
        action_ids = [action.id for action in self.actions]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resource_id values must be unique")
        if len(set(gpu_uuids)) != len(gpu_uuids):
            raise ValueError("gpu_uuid values must be unique within one host")
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("action ids must be unique")
        if any(
            resource.resource_id.partition("/")[0] != self.node_id
            for resource in self.resources
        ):
            raise ValueError("every resource_id must use the manifest node_id")
        if any(action.resource_id not in resource_ids for action in self.actions):
            raise ValueError("every action must target a declared resource")
        if self.scenario.startswith("single-card") and len(resource_ids) != 1:
            raise ValueError("single-card scenarios require exactly one resource")
        if self.scenario == "dual-card" and len(resource_ids) < 2:
            raise ValueError("dual-card scenario requires at least two resources")
        if self.scenario == "cross-host" and len(resource_ids) != 1:
            raise ValueError("cross-host scenario requires exactly one local resource")
        expected_rejections = [
            action for action in self.actions if action.expected_error_code is not None
        ]
        if self.scenario == "single-card-capacity-rejection":
            if (
                len(self.actions) != 1
                or len(expected_rejections) != 1
                or self.actions[0].role != "requester"
            ):
                raise ValueError(
                    "capacity-rejection requires one requester with an expected error"
                )
        elif expected_rejections:
            raise ValueError("expected_error_code is only valid for capacity-rejection")
        return self


@dataclass(frozen=True)
class BackendEndpoint:
    backend_id: str
    resource_id: str
    url: str
    auth_method: str
    auth_token: str | None

    def client_subject(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.UUID(self.backend_id),
            url=self.url,
            auth_method=self.auth_method,
            auth_token=self.auth_token,
            gpu_resource_id=self.resource_id,
            vram_budget_mb=1,
            extra_params={},
        )


@dataclass
class FaultController:
    kind: Literal[
        "response-lost-after-http",
        "cancel-after-grant",
        "health-timeout",
    ]
    target: str
    hits: int = 0
    hit_action_id: str | None = None

    def hit_action(self, action: ActionSpec) -> bool:
        if self.kind == "health-timeout" or action.id != self.target or self.hits:
            return False
        self.hits += 1
        self.hit_action_id = action.id
        return True

    def hit_health(self, backend_id: uuid.UUID) -> bool:
        if self.kind != "health-timeout" or str(backend_id) != self.target or self.hits:
            return False
        self.hits += 1
        self.hit_action_id = _CURRENT_ACTION_ID.get()
        return True


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        errors = [
            {
                "type": error["type"],
                "loc": list(error["loc"]),
                "msg": error["msg"],
            }
            for error in exc.errors(include_url=False, include_input=False)
        ]
        message = f"ValidationError: {json.dumps(errors, sort_keys=True)}"
    else:
        message = f"{type(exc).__name__}: {exc}"
    message = re.sub(r"(://)[^/@\s]+@", r"\1***@", message)
    message = re.sub(
        r"([?&][^=&\s]+)=([^&\s]*)",
        r"\1***",
        message,
    )
    message = re.sub(
        r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1***",
        message,
    )
    return re.sub(
        r"(?i)((?:api-key|x-api-key|x-auth-token)\s*:\s*)[^\s,;]+",
        r"\1***",
        message,
    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _strict_backend_residency(value: Any) -> BackendResidency:
    return BackendResidency.model_validate_json(_canonical_json(value), strict=True)


def _private_fingerprint(value: Any) -> str:
    return hmac.new(
        _PRIVATE_FINGERPRINT_KEY,
        _canonical_json(value),
        hashlib.sha256,
    ).hexdigest()


def _safe_endpoint(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    safe = urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    return {"origin": safe}


def _thresholds() -> dict[str, int | float]:
    return {
        "sample_count": SAMPLE_COUNT,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "stable_memory_spread_mb": STABLE_MEMORY_SPREAD_MB,
        "min_memory_recovery_ratio": MIN_MEMORY_RECOVERY_RATIO,
        "min_parallel_overlap_ms": MIN_PARALLEL_OVERLAP_MS,
    }


def _threshold_applicability() -> dict[str, str]:
    return {
        "min_memory_recovery_ratio": "isolated-full-unload-only",
    }


def _manifest_resources(manifest: ValidationManifest) -> list[dict[str, str]]:
    return [resource.model_dump() for resource in manifest.resources]


def _verification_action_body(operation: str) -> dict[str, Any]:
    if operation == "predict":
        return {"tasks": []}
    if operation == "predict_interactive":
        return {"task": {}, "context": {}}
    return {}


def _evidence_manifest(manifest: ValidationManifest) -> dict[str, Any]:
    payload = manifest.model_dump(mode="json")
    for action in payload["actions"]:
        action["body"] = _verification_action_body(action["operation"])
    return payload


def evaluate_stable_memory(samples_mb: Sequence[int]) -> dict[str, Any]:
    values = list(samples_mb)
    if len(values) < SAMPLE_COUNT:
        return {
            "passed": False,
            "reason": "insufficient_samples",
            "samples_mb": values,
        }
    spread = max(values) - min(values)
    return {
        "passed": spread <= STABLE_MEMORY_SPREAD_MB,
        "reason": "stable" if spread <= STABLE_MEMORY_SPREAD_MB else "spread_exceeded",
        "samples_mb": values,
        "median_mb": median(values),
        "spread_mb": spread,
    }


def evaluate_memory_recovery(
    *,
    context_samples_mb: Sequence[int],
    loaded_mb: int,
    unloaded_samples_mb: Sequence[int],
    gpu_total_mb: int,
) -> dict[str, Any]:
    context = evaluate_stable_memory(context_samples_mb)
    unloaded = evaluate_stable_memory(unloaded_samples_mb)
    if not context["passed"] or not unloaded["passed"]:
        return {"passed": False, "reason": "unstable_window"}
    context_median = float(context["median_mb"])
    unloaded_median = float(unloaded["median_mb"])
    working_set_mb = loaded_mb - context_median
    recovered_mb = loaded_mb - unloaded_median
    ratio = recovered_mb / working_set_mb if working_set_mb > 0 else 0.0
    slack_mb = max(512, int(gpu_total_mb * 0.02))
    passed = (
        unloaded_median <= context_median + slack_mb
        and working_set_mb > 0
        and ratio >= MIN_MEMORY_RECOVERY_RATIO
    )
    return {
        "passed": passed,
        "reason": "recovered" if passed else "recovery_below_threshold",
        "context_median_mb": context_median,
        "loaded_mb": loaded_mb,
        "unloaded_median_mb": unloaded_median,
        "working_set_mb": working_set_mb,
        "recovered_mb": recovered_mb,
        "recovery_ratio": ratio,
        "baseline_slack_mb": slack_mb,
    }


def action_overlap_ms(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    start = max(
        float(left["started_monotonic_ms"]), float(right["started_monotonic_ms"])
    )
    end = min(
        float(left["finished_monotonic_ms"]), float(right["finished_monotonic_ms"])
    )
    return max(0.0, end - start)


def parse_nvidia_smi(
    gpu_output: str,
    process_output: str,
) -> dict[str, Any]:
    gpus: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    seen_uuids: set[str] = set()
    for raw in gpu_output.splitlines():
        if not raw.strip():
            continue
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 4:
            raise ValueError("nvidia-smi GPU row must contain four columns")
        index = int(parts[0])
        gpu_uuid = parts[1]
        if index in seen_indexes or gpu_uuid in seen_uuids:
            raise ValueError("nvidia-smi GPU identity is duplicated")
        if _GPU_UUID_RE.fullmatch(gpu_uuid) is None:
            raise ValueError("nvidia-smi returned an invalid GPU UUID")
        seen_indexes.add(index)
        seen_uuids.add(gpu_uuid)
        gpus.append(
            {
                "index": index,
                "uuid": gpu_uuid,
                "memory_total_mb": int(parts[2]),
                "memory_used_mb": int(parts[3]),
            }
        )
    processes: list[dict[str, Any]] = []
    for raw in process_output.splitlines():
        if not raw.strip():
            continue
        parts = [part.strip() for part in raw.split(",", 3)]
        if len(parts) != 4 or parts[0] not in seen_uuids:
            raise ValueError("nvidia-smi process row identity is invalid")
        used_memory: int | None
        try:
            used_memory = int(parts[3])
        except ValueError:
            used_memory = None
        processes.append(
            {
                "gpu_uuid": parts[0],
                "pid": int(parts[1]),
                "process_name": parts[2],
                "used_memory_mb": used_memory,
            }
        )
    return {"gpus": gpus, "compute_processes": processes}


async def _command_output(*argv: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(NVIDIA_SMI_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            async with asyncio.timeout(SUBPROCESS_TERMINATE_TIMEOUT_SECONDS):
                await process.communicate()
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
        raise
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{argv[0]} failed: {message[:500]}")
    return stdout.decode("utf-8", errors="strict")


async def sample_nvidia_smi() -> dict[str, Any]:
    tasks = (
        asyncio.create_task(
            _command_output(
                "nvidia-smi",
                "--query-gpu=index,uuid,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            )
        ),
        asyncio.create_task(
            _command_output(
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            )
        ),
    )
    try:
        gpu_output, process_output = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return parse_nvidia_smi(gpu_output, process_output)


def load_manifest(path: Path) -> tuple[ValidationManifest, dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    manifest = ValidationManifest.model_validate(payload, strict=True)
    return manifest, payload, hashlib.sha256(_canonical_json(payload)).hexdigest()


def _registry_health_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: _json_safe(value[key])
        for key in (
            "gpu_arbiter_probe",
            "residency",
            "compute",
            "gpu_info",
        )
        if key in value
    }


async def collect_database_snapshot(
    session_factory,
    manifest: ValidationManifest,
) -> tuple[dict[str, Any], dict[str, BackendEndpoint]]:
    resource_ids = [resource.resource_id for resource in manifest.resources]
    requested_backend_ids = [
        uuid.UUID(action.backend_id) for action in manifest.actions
    ]
    async with session_factory() as db:
        await db.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        db_clock = await db.scalar(text("SELECT clock_timestamp()"))
        database_heads = sorted(
            str(value)
            for value in (
                await db.execute(text("SELECT version_num FROM alembic_version"))
            ).scalars()
        )
        memberships = list(
            (
                await db.execute(
                    select(GPUBackendMembership)
                    .where(GPUBackendMembership.gpu_resource_id.in_(resource_ids))
                    .order_by(
                        GPUBackendMembership.gpu_resource_id,
                        GPUBackendMembership.backend_registry_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        membership_backend_ids = {
            membership.backend_registry_id for membership in memberships
        }
        backend_ids = sorted(
            membership_backend_ids | set(requested_backend_ids), key=str
        )
        registries = list(
            (
                await db.execute(
                    select(MLBackendRegistry)
                    .where(
                        or_(
                            MLBackendRegistry.id.in_(backend_ids),
                            MLBackendRegistry.gpu_resource_id.in_(resource_ids),
                        )
                    )
                    .order_by(MLBackendRegistry.id)
                )
            )
            .scalars()
            .all()
        )
        fences = list(
            (
                await db.execute(
                    select(GPUBackendFence)
                    .where(GPUBackendFence.backend_registry_id.in_(backend_ids))
                    .order_by(GPUBackendFence.backend_registry_id)
                )
            )
            .scalars()
            .all()
        )
        cancel_intents = list(
            (
                await db.execute(
                    select(GPUBackendCancelIntent)
                    .where(GPUBackendCancelIntent.backend_registry_id.in_(backend_ids))
                    .order_by(GPUBackendCancelIntent.backend_registry_id)
                )
            )
            .scalars()
            .all()
        )

        endpoints = {
            str(registry.id): BackendEndpoint(
                backend_id=str(registry.id),
                resource_id=registry.gpu_resource_id or "",
                url=registry.url,
                auth_method=registry.auth_method,
                auth_token=registry.auth_token,
            )
            for registry in registries
        }
        registry_rows = [
            {
                "backend_id": str(registry.id),
                "name": registry.name,
                "endpoint": _safe_endpoint(registry.url),
                "state": registry.state,
                "auth_method": registry.auth_method,
                "gpu_resource_id": registry.gpu_resource_id,
                "vram_budget_mb": registry.vram_budget_mb,
                "eviction_priority": registry.eviction_priority,
                "max_concurrency": (registry.extra_params or {}).get(
                    "max_concurrency", 4
                ),
                "last_checked_at": _json_safe(registry.last_checked_at),
                "health": _registry_health_evidence(registry.health_meta),
            }
            for registry in registries
        ]
        membership_rows = [
            {
                "backend_id": str(item.backend_registry_id),
                "gpu_resource_id": item.gpu_resource_id,
                "membership_epoch": item.membership_epoch,
                "runtime_epoch_baseline": item.runtime_epoch_baseline,
                "state": item.state,
                "vram_budget_mb": item.vram_budget_mb,
                "eviction_priority": item.eviction_priority,
                "max_concurrency": item.max_concurrency,
                "retirement_id": _json_safe(item.retirement_id),
                "retire_reason": item.retire_reason,
            }
            for item in memberships
        ]
        fence_rows = [
            {
                "backend_id": str(item.backend_registry_id),
                "generation_high_water": item.generation_high_water,
                "control_epoch_high_water": item.control_epoch_high_water,
                "runtime_epoch_high_water": item.runtime_epoch_high_water,
                "token_expiry_high_water": _json_safe(item.token_expiry_high_water),
            }
            for item in fences
        ]
        cancel_rows = [
            {
                "backend_id": str(item.backend_registry_id),
                "gpu_resource_id": item.gpu_resource_id,
                "membership_epoch": item.membership_epoch,
                "boot_id": item.boot_id,
                "control_epoch": item.control_epoch,
                "runtime_epoch": item.runtime_epoch,
                "source_generation": item.source_generation,
                "drain_generation": item.drain_generation,
                "generation": item.generation,
                "owner_id": item.owner_id,
                "operation": item.operation,
                "owner_hard_deadline_ms": item.owner_hard_deadline_ms,
                "drain_token_expires_at": _json_safe(item.drain_token_expires_at),
                "token_expires_at": _json_safe(item.token_expires_at),
                "jti": item.jti,
                "pool_ids": list(item.pool_ids),
                "subject_fingerprint": item.subject_fingerprint,
            }
            for item in cancel_intents
        ]
        control_registry_rows = [
            {
                **{key: value for key, value in row.items() if key != "health"},
                "transport_fingerprint": _private_fingerprint(
                    {
                        "url": registry.url,
                        "auth_method": registry.auth_method,
                        "auth_token": registry.auth_token,
                    }
                ),
            }
            for row, registry in zip(registry_rows, registries, strict=True)
        ]
        topology = {
            "database_heads": database_heads,
            "registries": [
                {key: value for key, value in row.items() if key != "last_checked_at"}
                for row in control_registry_rows
            ],
            "memberships": membership_rows,
        }
        control = {
            "database_heads": database_heads,
            "registries": control_registry_rows,
            "memberships": membership_rows,
            "fences": fence_rows,
            "cancel_intents": cancel_rows,
        }
        snapshot = {
            "observed_at": _utc_now(),
            "database_clock": _json_safe(db_clock),
            "database_heads": database_heads,
            "registries": registry_rows,
            "memberships": membership_rows,
            "fences": fence_rows,
            "cancel_intents": cancel_rows,
            "topology_fingerprint": _sha256_json(topology),
            "control_fingerprint": _sha256_json(control),
        }
        await db.rollback()
    return snapshot, endpoints


async def collect_database_clock(session_factory) -> str:
    async with session_factory() as db:
        database_clock = await db.scalar(text("SELECT clock_timestamp()"))
        await db.rollback()
    return str(database_clock)


async def collect_redis_snapshot(
    store: GPUArbiterStore,
    manifest: ValidationManifest,
) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for resource in manifest.resources:
        try:
            prepared = await store.prepared_proof_reset(resource.resource_id)
            if prepared is not None:
                resources[resource.resource_id] = {
                    "status": "prepared",
                    "prepared": _json_safe(prepared),
                }
                continue
            snapshot = await store.snapshot(resource.resource_id)
            resources[resource.resource_id] = {
                "status": "ready" if snapshot.ready else "not_ready",
                "snapshot": _json_safe(snapshot),
            }
        except GPUArbiterStoreError as exc:
            resources[resource.resource_id] = {
                "status": "error",
                "error": str(exc),
            }
    return {"observed_at": _utc_now(), "resources": resources}


async def collect_live_backends(
    endpoints: Mapping[str, BackendEndpoint],
) -> dict[str, Any]:
    async def collect(endpoint: BackendEndpoint) -> tuple[str, dict[str, Any]]:
        challenge = secrets.token_hex(32)
        client = BackendTransport(
            endpoint.client_subject(),
            options=settings,
            dispatch_context=_readonly_dispatch,
            observe=observe_ml_backend,
        )
        setup: dict[str, Any] | None = None
        setup_error: str | None = None
        try:
            setup = await client.setup()
        except Exception as exc:  # noqa: BLE001 - evidence records the boundary
            setup_error = _safe_error(exc)
        try:
            healthy, meta = await client.health_meta(gpu_health_challenge=challenge)
        except Exception as exc:  # noqa: BLE001 - evidence records the boundary
            healthy, meta = False, None
            health_error = _safe_error(exc)
        else:
            health_error = None
        managed_lifecycle = setup.get("managed_lifecycle") if setup else None
        try:
            capability_sha256 = (
                managed_lifecycle_capability_sha256(managed_lifecycle)
                if managed_lifecycle is not None
                else None
            )
        except ValueError:
            capability_sha256 = None
        raw_residency = (meta or {}).get("residency")
        try:
            residency = (
                _strict_backend_residency(raw_residency).model_dump(mode="json")
                if raw_residency is not None
                else None
            )
        except ValueError as exc:
            residency = None
            residency_error = _safe_error(exc)
        else:
            residency_error = None
        return endpoint.backend_id, {
            "backend_id": endpoint.backend_id,
            "gpu_resource_id": endpoint.resource_id,
            "endpoint": _safe_endpoint(endpoint.url),
            "challenge": challenge,
            "challenge_echoed": bool(
                meta and meta.get(_GPU_HEALTH_CHALLENGE_ECHO_MARKER) == challenge
            ),
            "healthy": healthy,
            "health_error": health_error,
            "residency": residency,
            "residency_error": residency_error,
            "compute": _json_safe((meta or {}).get("compute")),
            "gpu_info": _json_safe((meta or {}).get("gpu_info")),
            "setup_error": setup_error,
            "setup": (
                {
                    key: _json_safe(setup[key])
                    for key in (
                        "protocol_version",
                        "compat_protocol_versions",
                        "name",
                        "version",
                        "model_version",
                        "managed_lifecycle",
                    )
                    if key in setup
                }
                if setup
                else None
            ),
            "managed_lifecycle_sha256": capability_sha256,
        }

    rows = await asyncio.gather(*(collect(endpoint) for endpoint in endpoints.values()))
    return {backend_id: row for backend_id, row in rows}


def _code_database_heads() -> list[str]:
    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    api_root = Path(__file__).resolve().parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


def _check(
    code: str,
    passed: bool,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": "passed" if passed else "blocked",
        "message": message,
        "details": _json_safe(details),
    }


def _backend_physical_gpu_exact(
    live: Mapping[str, Any],
    *,
    gpu_uuid: str,
    gpu_index: int,
) -> bool:
    gpu_info = live.get("gpu_info")
    if (
        not isinstance(gpu_info, Mapping)
        or not isinstance(gpu_index, int)
        or isinstance(gpu_index, bool)
    ):
        return False

    observed = False
    observed_uuid = gpu_info.get("device_uuid")
    if observed_uuid is not None:
        observed = True
        if observed_uuid != gpu_uuid:
            return False

    observed_index = gpu_info.get("device_index")
    if observed_index is not None:
        observed = True
        if (
            not isinstance(observed_index, int)
            or isinstance(observed_index, bool)
            or observed_index != gpu_index
        ):
            return False

    physical_device_token = gpu_info.get("physical_device_token")
    if physical_device_token is not None:
        observed = True
        if not isinstance(physical_device_token, str):
            return False
        if physical_device_token.startswith("index:"):
            raw_index = physical_device_token.removeprefix("index:")
            if not raw_index.isdigit() or int(raw_index) != gpu_index:
                return False
        elif physical_device_token != gpu_uuid:
            return False
    return observed


def evaluate_preflight(
    *,
    manifest: ValidationManifest,
    config: Settings,
    database: Mapping[str, Any],
    redis_snapshot: Mapping[str, Any],
    live_backends: Mapping[str, Any],
    nvidia: Mapping[str, Any],
    code_database_heads: Sequence[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "gpu_config_parse",
            not config.gpu_arbiter_config_errors,
            "GPU resource config must parse without errors",
            errors=config.gpu_arbiter_config_errors,
        )
    )
    configured = config.gpu_arbiter_resources
    visible_gpus = {item["uuid"]: item for item in nvidia.get("gpus", [])}
    registry_by_id = {row["backend_id"]: row for row in database.get("registries", [])}
    membership_by_backend = {
        row["backend_id"]: row for row in database.get("memberships", [])
    }
    fence_by_backend = {row["backend_id"]: row for row in database.get("fences", [])}
    validation_resource_by_backend = {
        action.backend_id: action.resource_id for action in manifest.actions
    }
    allocation_by_backend: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for resource in manifest.resources:
        configured_resource = configured.get(resource.resource_id)
        config_matches = bool(
            configured_resource
            and configured_resource.node_id == manifest.node_id
            and (
                configured_resource.physical_device_token == resource.gpu_uuid
                or configured_resource.physical_device_token.startswith("index:")
            )
        )
        checks.append(
            _check(
                "resource_config_exact",
                config_matches,
                "manifest resource must match GPU_ARBITER_RESOURCES_JSON",
                resource_id=resource.resource_id,
            )
        )
        desired_enforce = (
            configured_resource is not None
            and config.gpu_arbiter_desired_mode(resource.resource_id)
            is GPUArbiterMode.ENFORCE
        )
        checks.append(
            _check(
                "resource_desired_enforce",
                desired_enforce,
                "validation authority requires desired enforce configuration",
                resource_id=resource.resource_id,
            )
        )
        gpu = visible_gpus.get(resource.gpu_uuid)
        if gpu is not None and configured_resource is not None:
            token = configured_resource.physical_device_token
            if token.startswith("index:"):
                gpu = gpu if gpu["index"] == int(token.partition(":")[2]) else None
        checks.append(
            _check(
                "physical_gpu_exact",
                gpu is not None,
                "manifest GPU UUID and configured physical token must identify one visible GPU",
                resource_id=resource.resource_id,
                gpu_uuid=resource.gpu_uuid,
            )
        )
        redis_resource = redis_snapshot.get("resources", {}).get(
            resource.resource_id, {}
        )
        snapshot = redis_resource.get("snapshot") or {}
        for allocation in snapshot.get("allocations", []):
            backend_id = allocation["backend_id"]
            allocation_by_backend[backend_id] = (resource.resource_id, allocation)
            validation_resource_by_backend.setdefault(backend_id, resource.resource_id)
        redis_ready = redis_resource.get("status") == "ready"
        checks.append(
            _check(
                "redis_ready",
                redis_ready,
                "Redis ledger must be a ready, validated v3 snapshot",
                resource_id=resource.resource_id,
                status=redis_resource.get("status"),
                error=redis_resource.get("error"),
            )
        )
        if snapshot:
            allocatable_exact = bool(
                configured_resource is not None
                and snapshot.get("allocatable_mb") == configured_resource.allocatable_mb
            )
            checks.append(
                _check(
                    "redis_allocatable_exact",
                    allocatable_exact,
                    "Redis allocatable capacity must exactly match static configuration",
                    resource_id=resource.resource_id,
                    redis_allocatable_mb=snapshot.get("allocatable_mb"),
                    configured_allocatable_mb=(
                        configured_resource.allocatable_mb
                        if configured_resource is not None
                        else None
                    ),
                )
            )
            checks.append(
                _check(
                    "redis_budget_invariant",
                    snapshot["committed_mb"] <= snapshot["allocatable_mb"],
                    "committed_mb must not exceed allocatable_mb",
                    resource_id=resource.resource_id,
                    committed_mb=snapshot["committed_mb"],
                    allocatable_mb=snapshot["allocatable_mb"],
                )
            )
            durable_domain = sorted(
                (
                    {
                        "backend_id": row["backend_id"],
                        "membership_epoch": row["membership_epoch"],
                        "state": row["state"],
                    }
                    for row in database.get("memberships", [])
                    if row["gpu_resource_id"] == resource.resource_id
                ),
                key=lambda item: item["backend_id"],
            )
            checks.append(
                _check(
                    "redis_membership_domain_exact",
                    snapshot.get("backend_memberships") == durable_domain,
                    "Redis membership domain must exactly match PostgreSQL",
                    resource_id=resource.resource_id,
                )
            )
            runtime_clean = bool(
                not snapshot.get("leases")
                and not snapshot.get("card_queue")
                and not snapshot.get("backend_queues")
                and snapshot.get("transition") is None
                and all(
                    allocation.get("state") in {"resident", "unloaded"}
                    for allocation in snapshot.get("allocations", [])
                )
            )
            checks.append(
                _check(
                    "initial_runtime_clean",
                    runtime_clean,
                    "validation must start without leases, queues, transitions, or uncertain allocations",
                    resource_id=resource.resource_id,
                )
            )
            allocation_claims_exact = all(
                (membership := membership_by_backend.get(allocation["backend_id"]))
                is not None
                and (registry := registry_by_id.get(allocation["backend_id"]))
                is not None
                and allocation.get("budget_mb")
                == membership.get("vram_budget_mb")
                == registry.get("vram_budget_mb")
                and allocation.get("eviction_priority")
                == membership.get("eviction_priority")
                == registry.get("eviction_priority")
                and allocation.get("max_concurrency")
                == membership.get("max_concurrency")
                == registry.get("max_concurrency")
                for allocation in snapshot.get("allocations", [])
            )
            checks.append(
                _check(
                    "redis_allocation_claims_exact",
                    allocation_claims_exact,
                    "Redis allocation claims must exactly match durable backend claims",
                    resource_id=resource.resource_id,
                )
            )
    checks.append(
        _check(
            "database_schema_head",
            sorted(database.get("database_heads", [])) == sorted(code_database_heads),
            "database Alembic head must match checked-out code",
            database_heads=database.get("database_heads", []),
            code_heads=list(code_database_heads),
        )
    )
    for backend_id, resource_id in sorted(validation_resource_by_backend.items()):
        registry = registry_by_id.get(backend_id)
        membership = membership_by_backend.get(backend_id)
        fence = fence_by_backend.get(backend_id)
        allocation_entry = allocation_by_backend.get(backend_id)
        live = live_backends.get(backend_id) or {}
        claim_ok = bool(
            registry
            and registry["gpu_resource_id"] == resource_id
            and isinstance(registry["vram_budget_mb"], int)
            and registry["vram_budget_mb"] > 0
        )
        checks.append(
            _check(
                "backend_claim_exact",
                claim_ok,
                "in-scope backend must have one exact registry claim",
                backend_id=backend_id,
                resource_id=resource_id,
            )
        )
        membership_ok = bool(
            membership
            and membership["gpu_resource_id"] == resource_id
            and membership["state"] == "active"
        )
        checks.append(
            _check(
                "backend_membership_active",
                membership_ok,
                "in-scope backend membership must be active on the exact resource",
                backend_id=backend_id,
            )
        )
        try:
            allocation_generation = (
                allocation_entry[1]["generation"] if allocation_entry else None
            )
            fence_ok = bool(
                fence
                and int(fence["generation_high_water"]) >= 0
                and int(fence["control_epoch_high_water"]) > 0
                and int(fence["runtime_epoch_high_water"]) > 0
                and (
                    allocation_entry is None
                    or (
                        allocation_entry[0] == resource_id
                        and int(fence["generation_high_water"])
                        >= int(allocation_generation)
                    )
                )
            )
        except (KeyError, TypeError, ValueError):
            fence_ok = False
        checks.append(
            _check(
                "backend_fence_active",
                fence_ok,
                "in-scope backend fence must be active and cover its allocation generation when present",
                backend_id=backend_id,
            )
        )
        residency = live.get("residency") or {}
        identity = residency.get("identity") or {}
        probe = (registry or {}).get("health") or {}
        probe = probe.get("gpu_arbiter_probe") or {}
        live_ok = bool(
            live.get("healthy")
            and live.get("challenge_echoed")
            and live.get("managed_lifecycle_sha256")
            and live.get("managed_lifecycle_sha256")
            == probe.get("managed_lifecycle_sha256")
            and identity.get("backend_registry_id") == backend_id
            and identity.get("gpu_resource_id") == resource_id
            and residency.get("lifecycle_gate") == "enforce"
        )
        checks.append(
            _check(
                "backend_live_proof",
                live_ok,
                "backend setup and challenge-bound health must prove exact managed identity",
                backend_id=backend_id,
            )
        )
        resource = next(
            item for item in manifest.resources if item.resource_id == resource_id
        )
        physical_gpu = visible_gpus.get(resource.gpu_uuid)
        checks.append(
            _check(
                "backend_physical_gpu_exact",
                bool(
                    physical_gpu is not None
                    and _backend_physical_gpu_exact(
                        live,
                        gpu_uuid=resource.gpu_uuid,
                        gpu_index=physical_gpu["index"],
                    )
                ),
                "backend health must prove the exact physical GPU UUID or index",
                backend_id=backend_id,
                resource_id=resource_id,
            )
        )
    return checks


def _database_window_check(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    before_fingerprint = before.get("control_fingerprint")
    after_fingerprint = after.get("control_fingerprint")
    return _check(
        "database_control_window_stable",
        bool(before_fingerprint) and before_fingerprint == after_fingerprint,
        "database control state must remain stable across external evidence sampling",
        before_database_clock=before.get("database_clock"),
        after_database_clock=after.get("database_clock"),
        before_control_fingerprint=before_fingerprint,
        after_control_fingerprint=after_fingerprint,
    )


async def collect_preflight(
    *,
    manifest: ValidationManifest,
    manifest_sha256: str,
    session_factory,
    config: Settings,
    store: GPUArbiterStore,
) -> tuple[dict[str, Any], dict[str, BackendEndpoint]]:
    for snapshot_attempt in range(1, PREFLIGHT_SNAPSHOT_ATTEMPTS + 1):
        database_before, endpoints_before = await collect_database_snapshot(
            session_factory, manifest
        )
        redis_snapshot, live_backends, nvidia = await asyncio.gather(
            collect_redis_snapshot(store, manifest),
            collect_live_backends(endpoints_before),
            sample_nvidia_smi(),
        )
        database_after, endpoints_after = await collect_database_snapshot(
            session_factory, manifest
        )
        window_check = _database_window_check(database_before, database_after)
        if window_check["status"] == "passed":
            break
    try:
        signer = GPUAdmissionTokenSigner.from_settings(_owner_settings(config))
    except Exception as exc:  # noqa: BLE001 - reported as a preflight blocker
        signer_check = _check(
            "signer_ready",
            False,
            "GPU lifecycle signer must be configured",
            error=_safe_error(exc),
        )
    else:
        signer_check = _check(
            "signer_ready",
            True,
            "GPU lifecycle signer is configured",
            active_kid=signer.active_kid,
        )
    code_heads = _code_database_heads()
    checks = [signer_check, window_check]
    checks.extend(
        evaluate_preflight(
            manifest=manifest,
            config=config,
            database=database_after,
            redis_snapshot=redis_snapshot,
            live_backends=live_backends,
            nvidia=nvidia,
            code_database_heads=code_heads,
        )
    )
    passed = all(check["status"] == "passed" for check in checks)
    report = {
        "schema": EVIDENCE_SCHEMA,
        "command": "preflight",
        "status": "passed" if passed else "blocked",
        "observed_at": _utc_now(),
        "manifest_sha256": manifest_sha256,
        "cohort_id": manifest.cohort_id,
        "node_id": manifest.node_id,
        "scenario": manifest.scenario,
        "resources": _manifest_resources(manifest),
        "environment": config.environment,
        "thresholds": _thresholds(),
        "threshold_applicability": _threshold_applicability(),
        "checks": checks,
        "snapshot": {
            "database": database_after,
            "database_window": {
                "attempt": snapshot_attempt,
                "max_attempts": PREFLIGHT_SNAPSHOT_ATTEMPTS,
                "before": {
                    key: database_before.get(key)
                    for key in (
                        "observed_at",
                        "database_clock",
                        "control_fingerprint",
                    )
                },
                "after": {
                    key: database_after.get(key)
                    for key in (
                        "observed_at",
                        "database_clock",
                        "control_fingerprint",
                    )
                },
            },
            "redis": redis_snapshot,
            "backends": live_backends,
            "nvidia_smi": nvidia,
        },
    }
    return report, endpoints_after


async def refresh_runtime_proofs(
    backend_ids: Sequence[str],
    *,
    refresher=None,
) -> list[dict[str, Any]]:
    """Persist fresh challenge proofs before the mutating validation run."""

    if refresher is None:
        from aap_inference.gpu_health import PersistedGPUHealthRefresher
        from app.services.inference_limits import capability_limits_from_settings

        refresher = PersistedGPUHealthRefresher(
            _owner_database_url("INFERENCE_HEALTH_DATA_DATABASE_URL"),
            options=settings,
            capability_limits_factory=capability_limits_from_settings,
        )

    checks: list[dict[str, Any]] = []
    for backend_id in sorted(set(backend_ids)):
        try:
            refreshed = await refresher(
                uuid.UUID(backend_id),
                secrets.token_hex(32),
            )
        except Exception as exc:  # noqa: BLE001 - evidence must fail closed
            checks.append(
                _check(
                    "run_runtime_proof_refreshed",
                    False,
                    "run must persist fresh challenge-bound health before dispatch",
                    backend_id=backend_id,
                    error=_safe_error(exc),
                )
            )
        else:
            checks.append(
                _check(
                    "run_runtime_proof_refreshed",
                    refreshed is True,
                    "run must persist fresh challenge-bound health before dispatch",
                    backend_id=backend_id,
                )
            )
    return checks


def _preflight_allows_runtime_proof_refresh(report: Mapping[str, Any]) -> bool:
    """Allow run to repair only cached proof blockers after all static gates pass."""

    checks = report.get("checks")
    return bool(
        isinstance(checks, list)
        and checks
        and all(isinstance(check, Mapping) for check in checks)
        and all(
            check.get("status") == "passed" or check.get("code") == "backend_live_proof"
            for check in checks
        )
    )


def _validate_run_safety(
    *,
    environment: str,
    run_id: str,
    confirm_run_id: str,
) -> None:
    if environment == "production":
        raise PermissionError("run is forbidden when ENVIRONMENT=production")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9._-]{1,128}")
    if run_id != confirm_run_id:
        raise PermissionError("--confirm-run-id must exactly match --run-id")


async def _run_action(
    action: ActionSpec,
    *,
    endpoint: BackendEndpoint,
    dispatch_factory,
    database_clock,
    fault: FaultController | None,
) -> dict[str, Any]:
    if action.delay_ms:
        await asyncio.sleep(action.delay_ms / 1000)
    action_context = _CURRENT_ACTION_ID.set(action.id)
    started_at = _utc_now()
    started_monotonic_ms = time.monotonic() * 1000
    row: dict[str, Any] = {
        "id": action.id,
        "role": action.role,
        "backend_id": action.backend_id,
        "resource_id": action.resource_id,
        "operation": action.operation,
        "started_at": started_at,
        "started_monotonic_ms": started_monotonic_ms,
    }
    request = GPUDispatchRequest(
        request_id=uuid.uuid4(),
        backend_id=action.backend_id,
        gpu_resource_id=action.resource_id,
        operation=action.operation,
        scope=_ACTION_SCOPE[action.operation],
    )
    try:
        async with asyncio.timeout(action.timeout_seconds):
            async with dispatch_factory(request) as grant:
                row["grant_generation"] = grant.generation
                headers = {"Content-Type": "application/json"}
                if endpoint.auth_method == "token" and endpoint.auth_token:
                    headers["Authorization"] = f"Bearer {endpoint.auth_token}"
                headers[GPU_GENERATION_HEADER] = grant.generation
                headers[GPU_ADMISSION_TOKEN_HEADER] = grant.admission_token
                async with httpx.AsyncClient(timeout=action.timeout_seconds) as client:
                    database_probe_started_ms = time.monotonic() * 1000
                    row["http_started_database_clock"] = await database_clock()
                    row["http_started_database_probe_rtt_ms"] = (
                        time.monotonic() * 1000 - database_probe_started_ms
                    )
                    row["http_started_monotonic_ms"] = time.monotonic() * 1000
                    request_task = asyncio.create_task(
                        client.post(
                            f"{endpoint.url.rstrip('/')}{_ACTION_PATH[action.operation]}",
                            json=action.body,
                            headers=headers,
                        )
                    )
                    try:
                        if (
                            fault is not None
                            and fault.kind == "cancel-after-grant"
                            and fault.target == action.id
                            and fault.hits == 0
                        ):
                            await asyncio.sleep(0)
                            assert fault.hit_action(action)
                            request_task.cancel()
                            await asyncio.gather(request_task, return_exceptions=True)
                            raise asyncio.CancelledError("injected cancel-after-grant")
                        response = await request_task
                    finally:
                        if not request_task.done():
                            request_task.cancel()
                        await asyncio.gather(request_task, return_exceptions=True)
                row["http_finished_monotonic_ms"] = time.monotonic() * 1000
                database_probe_started_ms = time.monotonic() * 1000
                row["http_finished_database_clock"] = await database_clock()
                row["http_finished_database_probe_rtt_ms"] = (
                    time.monotonic() * 1000 - database_probe_started_ms
                )
                if (
                    fault is not None
                    and fault.kind == "response-lost-after-http"
                    and fault.hit_action(action)
                ):
                    raise TimeoutError("injected response-lost-after-http")
                grant.report_response(response.status_code)
                row.update(
                    {
                        "http_status": response.status_code,
                        "response_bytes": len(response.content),
                        "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    }
                )
                response.raise_for_status()
        row["status"] = "passed"
    except asyncio.CancelledError as exc:
        if (
            fault is None
            or fault.kind != "cancel-after-grant"
            or fault.hits != 1
            or fault.hit_action_id != action.id
        ):
            raise
        row.update(
            {
                "status": "fault_injected",
                "fault": fault.kind,
                "error": _safe_error(exc),
            }
        )
    except GPUArbiterDispatchError as exc:
        injected = bool(
            fault
            and fault.kind == "health-timeout"
            and fault.hits == 1
            and fault.hit_action_id == action.id
        )
        expected = action.expected_error_code
        matched = bool(
            not injected
            and expected is not None
            and exc.error_code == expected
            and "grant_generation" not in row
            and "http_started_monotonic_ms" not in row
        )
        row.update(
            {
                "status": (
                    "fault_injected" if injected else "passed" if matched else "failed"
                ),
                "fault": fault.kind if injected and fault else None,
                "expected_error_code": expected,
                "error_code": exc.error_code,
                "error_http_status": exc.status_code,
                "retry_after_seconds": exc.retry_after_s,
                "error": None if matched else _safe_error(exc),
            }
        )
    except Exception as exc:  # noqa: BLE001 - evidence records action failure
        injected = bool(
            fault
            and fault.hits == 1
            and fault.hit_action_id == action.id
            and fault.kind in {"response-lost-after-http", "health-timeout"}
        )
        row.update(
            {
                "status": "fault_injected" if injected else "failed",
                "fault": fault.kind if injected and fault else None,
                "error": _safe_error(exc),
            }
        )
    finally:
        row["finished_at"] = _utc_now()
        row["finished_monotonic_ms"] = time.monotonic() * 1000
        _CURRENT_ACTION_ID.reset(action_context)
    return row


async def _gpu_samples(count: int = SAMPLE_COUNT) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(count):
        samples.append({"observed_at": _utc_now(), **await sample_nvidia_smi()})
        if index + 1 < count:
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
    return samples


async def _runtime_sample(
    *,
    store: GPUArbiterStore,
    manifest: ValidationManifest,
    endpoints: Mapping[str, BackendEndpoint],
) -> dict[str, Any]:
    redis_snapshot, live_backends, nvidia = await asyncio.gather(
        collect_redis_snapshot(store, manifest),
        collect_live_backends(endpoints),
        sample_nvidia_smi(),
    )
    return {
        "observed_at": _utc_now(),
        "redis": redis_snapshot,
        "backends": live_backends,
        "nvidia_smi": nvidia,
    }


def _memory_series(samples: Sequence[Mapping[str, Any]], gpu_uuid: str) -> list[int]:
    values: list[int] = []
    for sample in samples:
        nvidia = sample.get("nvidia_smi", sample)
        for gpu in nvidia.get("gpus", []):
            if gpu.get("uuid") == gpu_uuid:
                values.append(int(gpu["memory_used_mb"]))
                break
    return values


def _fault_target_backend_ids(
    manifest: ValidationManifest,
    fault: FaultController | None,
) -> set[str]:
    if fault is None:
        return set()
    if fault.kind == "health-timeout":
        return set()
    return {
        action.backend_id for action in manifest.actions if action.id == fault.target
    }


def _timestamp_not_regressed(before: Any, after: Any) -> bool:
    if before is None:
        if after is None:
            return True
        try:
            datetime.fromisoformat(str(after).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return True
    if after is None:
        return False
    try:
        before_value = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
        after_value = datetime.fromisoformat(str(after).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return after_value >= before_value


def _fence_not_regressed(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> bool:
    if before is None or after is None:
        return False
    try:
        counters_not_regressed = all(
            int(after[field]) >= int(before[field])
            for field in (
                "generation_high_water",
                "control_epoch_high_water",
                "runtime_epoch_high_water",
            )
        )
    except (KeyError, TypeError, ValueError):
        return False
    return counters_not_regressed and _timestamp_not_regressed(
        before.get("token_expiry_high_water"),
        after.get("token_expiry_high_water"),
    )


def _final_truth_checks(
    *,
    manifest: ValidationManifest,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fault: FaultController | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    before_database = before.get("database", {})
    after_database = after.get("database", {})
    checks.append(
        _check(
            "run_topology_stable",
            bool(before_database.get("topology_fingerprint"))
            and before_database.get("topology_fingerprint")
            == after_database.get("topology_fingerprint"),
            "registry claims and membership topology must remain stable during the run",
            before_topology_fingerprint=before_database.get("topology_fingerprint"),
            after_topology_fingerprint=after_database.get("topology_fingerprint"),
        )
    )
    before_fences = {
        row["backend_id"]: row for row in before_database.get("fences", [])
    }
    after_fences = {row["backend_id"]: row for row in after_database.get("fences", [])}
    requested_backend_ids = {action.backend_id for action in manifest.actions}
    expected_rejection_backend_ids = {
        action.backend_id
        for action in manifest.actions
        if action.expected_error_code is not None
    }
    if fault is not None and fault.kind == "health-timeout":
        expected_rejection_backend_ids.update(
            action.backend_id
            for action in manifest.actions
            if action.id == fault.hit_action_id
        )
    before_allocation_backend_ids = {
        allocation["backend_id"]
        for resource in before.get("redis", {}).get("resources", {}).values()
        for allocation in (resource.get("snapshot") or {}).get("allocations", [])
    }
    allocations = {
        allocation["backend_id"]: (resource_id, allocation)
        for resource_id, resource in after.get("redis", {}).get("resources", {}).items()
        for allocation in (resource.get("snapshot") or {}).get("allocations", [])
    }
    truth_backend_ids = (
        requested_backend_ids | before_allocation_backend_ids | set(allocations)
    )
    fence_monotonic_by_backend = {
        backend_id: _fence_not_regressed(
            before_fences.get(backend_id), after_fences.get(backend_id)
        )
        for backend_id in truth_backend_ids
    }
    fence_monotonic = all(fence_monotonic_by_backend.values())
    checks.append(
        _check(
            "run_fence_monotonic",
            fence_monotonic,
            "durable generation and control/runtime fences must not regress",
        )
    )

    memberships = after_database.get("memberships", [])
    for resource in manifest.resources:
        redis_resource = (
            after.get("redis", {}).get("resources", {}).get(resource.resource_id, {})
        )
        snapshot = redis_resource.get("snapshot") or {}
        durable_domain = sorted(
            (
                {
                    "backend_id": row["backend_id"],
                    "membership_epoch": row["membership_epoch"],
                    "state": row["state"],
                }
                for row in memberships
                if row["gpu_resource_id"] == resource.resource_id
            ),
            key=lambda item: item["backend_id"],
        )
        checks.append(
            _check(
                "final_membership_domain_exact",
                redis_resource.get("status") == "ready"
                and snapshot.get("backend_memberships") == durable_domain,
                "final Redis membership domain must exactly match PostgreSQL",
                resource_id=resource.resource_id,
            )
        )

    registries = {
        row["backend_id"]: row for row in after_database.get("registries", [])
    }
    memberships_by_backend = {row["backend_id"]: row for row in memberships}
    target_fault_backends = _fault_target_backend_ids(manifest, fault)
    live_backends = after.get("backends", {})
    resources_by_id = {
        resource.resource_id: resource for resource in manifest.resources
    }
    physical_gpus = {
        gpu.get("uuid"): gpu
        for gpu in after.get("nvidia_smi", {}).get("gpus", [])
        if isinstance(gpu, Mapping)
    }
    for backend_id in sorted(truth_backend_ids):
        if backend_id in target_fault_backends:
            continue
        registry = registries.get(backend_id) or {}
        membership = memberships_by_backend.get(backend_id) or {}
        fence = after_fences.get(backend_id) or {}
        allocation_entry = allocations.get(backend_id)
        live = live_backends.get(backend_id) or {}
        try:
            residency = _strict_backend_residency(live.get("residency")).model_dump(
                mode="json"
            )
        except ValueError:
            residency = {}
        identity = residency.get("identity") or {}
        allocation_absent_expected = bool(
            allocation_entry is None and backend_id in expected_rejection_backend_ids
        )
        allocation_resource_id = (
            allocation_entry[0]
            if allocation_entry
            else registry.get("gpu_resource_id")
            if allocation_absent_expected
            else None
        )
        allocation = allocation_entry[1] if allocation_entry else {}
        resource = resources_by_id.get(allocation_resource_id)
        physical_gpu = physical_gpus.get(resource.gpu_uuid) if resource else None
        allocation_generation = (
            allocation.get("generation")
            if allocation_entry
            else residency.get("generation")
        )
        try:
            fence_covers_generation = bool(
                (allocation_absent_expected and allocation_generation is None)
                or (
                    allocation_generation is not None
                    and int(fence.get("generation_high_water", 0))
                    >= int(allocation_generation)
                )
            )
        except (TypeError, ValueError):
            fence_covers_generation = False
        probe = (registry.get("health") or {}).get("gpu_arbiter_probe") or {}
        if allocation_absent_expected:
            allocation_claim_exact = bool(
                membership.get("vram_budget_mb") == registry.get("vram_budget_mb")
                and membership.get("eviction_priority")
                == registry.get("eviction_priority")
                and membership.get("max_concurrency") == registry.get("max_concurrency")
            )
        else:
            allocation_claim_exact = bool(
                allocation.get("budget_mb")
                == membership.get("vram_budget_mb")
                == registry.get("vram_budget_mb")
                and allocation.get("eviction_priority")
                == membership.get("eviction_priority")
                == registry.get("eviction_priority")
                and allocation.get("max_concurrency")
                == membership.get("max_concurrency")
                == registry.get("max_concurrency")
            )
        common_exact = bool(
            registry.get("gpu_resource_id") == allocation_resource_id
            and membership.get("gpu_resource_id") == allocation_resource_id
            and membership.get("state") == "active"
            and live.get("healthy")
            and live.get("challenge_echoed")
            and live.get("managed_lifecycle_sha256")
            and live.get("managed_lifecycle_sha256")
            == probe.get("managed_lifecycle_sha256")
            and identity.get("backend_registry_id") == backend_id
            and identity.get("gpu_resource_id") == allocation_resource_id
            and residency.get("lifecycle_gate") == "enforce"
            and residency.get("generation") == allocation_generation
            and fence_monotonic_by_backend.get(backend_id, False)
            and fence_covers_generation
            and allocation_claim_exact
            and physical_gpu is not None
            and _backend_physical_gpu_exact(
                live,
                gpu_uuid=resource.gpu_uuid,
                gpu_index=physical_gpu.get("index"),
            )
        )
        state = "unloaded" if allocation_absent_expected else allocation.get("state")
        if state == "resident":
            pools = residency.get("pools") or {}
            state_exact = bool(
                residency.get("state") == "resident"
                and residency.get("gpu_loaded") is True
                and residency.get("active_requests") == 0
                and residency.get("builders") == 0
                and residency.get("borrowers") == 0
                and residency.get("draining") is False
                and residency.get("evictable") is True
                and pools
                and all(pool.get("resident") is not None for pool in pools.values())
                and any(pool.get("resident") is True for pool in pools.values())
            )
        elif state == "unloaded":
            pools = residency.get("pools") or {}
            state_exact = bool(
                residency.get("state") == "unloaded"
                and residency.get("gpu_loaded") is False
                and residency.get("active_requests") == 0
                and residency.get("builders") == 0
                and residency.get("borrowers") == 0
                and residency.get("draining") is False
                and residency.get("evictable") is False
                and pools
                and all(pool.get("resident") is False for pool in pools.values())
            )
        else:
            state_exact = False
        checks.append(
            _check(
                "final_backend_truth_exact",
                common_exact and state_exact,
                (
                    "final Redis allocation must match strict challenge-bound backend truth"
                    if allocation_entry is not None
                    else "safe allocation absence must match challenge-bound backend truth"
                ),
                backend_id=backend_id,
                resource_id=allocation_resource_id,
                allocation_state=state,
                residency_state=residency.get("state"),
            )
        )
    return checks


def _during_allocation_states(
    samples: Sequence[Mapping[str, Any]],
    *,
    resource_id: str,
    backend_id: str,
) -> set[str]:
    return {
        allocation["state"]
        for sample in samples
        for allocation in (
            sample.get("redis", {})
            .get("resources", {})
            .get(resource_id, {})
            .get("snapshot", {})
            .get("allocations", [])
        )
        if allocation.get("backend_id") == backend_id
        and isinstance(allocation.get("state"), str)
    }


def _resource_gpu_execution_observed(
    samples: Sequence[Mapping[str, Any]],
    *,
    resource_id: str,
    backend_ids: set[str],
) -> bool:
    for sample in samples:
        allocations = {
            allocation.get("backend_id"): allocation
            for allocation in (
                sample.get("redis", {})
                .get("resources", {})
                .get(resource_id, {})
                .get("snapshot", {})
                .get("allocations", [])
            )
        }
        live_backends = sample.get("backends", {})
        for backend_id in backend_ids:
            allocation = allocations.get(backend_id) or {}
            live = live_backends.get(backend_id) or {}
            try:
                residency = _strict_backend_residency(live.get("residency"))
            except ValueError:
                continue
            if (
                allocation.get("state") == "resident"
                and residency.state.value == "resident"
                and residency.gpu_loaded is True
                and residency.identity is not None
                and residency.identity.backend_registry_id == backend_id
                and residency.identity.gpu_resource_id == resource_id
            ):
                return True
    return False


def _snapshot_allocation(
    runtime: Mapping[str, Any],
    *,
    resource_id: str,
    backend_id: str,
) -> Mapping[str, Any] | None:
    return next(
        (
            allocation
            for allocation in (
                runtime.get("redis", {})
                .get("resources", {})
                .get(resource_id, {})
                .get("snapshot", {})
                .get("allocations", [])
            )
            if allocation.get("backend_id") == backend_id
        ),
        None,
    )


def evaluate_run(
    *,
    manifest: ValidationManifest,
    actions: Sequence[Mapping[str, Any]],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    baseline_samples: Sequence[Mapping[str, Any]],
    during_samples: Sequence[Mapping[str, Any]],
    recovery_samples: Sequence[Mapping[str, Any]],
    fault: FaultController | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    manifest_actions = {action.id: action for action in manifest.actions}
    result_actions = {
        str(row.get("id")): row
        for row in actions
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    action_results_exact = bool(
        len(result_actions) == len(actions) == len(manifest_actions)
        and set(result_actions) == set(manifest_actions)
        and all(
            row.get("backend_id") == manifest_actions[action_id].backend_id
            and row.get("resource_id") == manifest_actions[action_id].resource_id
            and row.get("operation") == manifest_actions[action_id].operation
            and row.get("role") == manifest_actions[action_id].role
            and (
                row.get("status") == "fault_injected"
                if fault is not None and action_id == fault.hit_action_id
                else (
                    row.get("status") == "passed"
                    and (
                        (
                            manifest_actions[action_id].expected_error_code is None
                            and row.get("expected_error_code") is None
                        )
                        or (
                            row.get("expected_error_code")
                            == manifest_actions[action_id].expected_error_code
                            == row.get("error_code")
                            and row.get("error_http_status") == 503
                            and row.get("grant_generation") is None
                            and row.get("http_status") is None
                            and row.get("http_started_monotonic_ms") is None
                        )
                    )
                )
            )
            for action_id, row in result_actions.items()
        )
    )
    checks.append(
        _check(
            "actions_completed",
            action_results_exact,
            "action results must exactly match the manifest and expected statuses",
            expected_action_ids=sorted(manifest_actions),
            observed_action_ids=sorted(result_actions),
        )
    )
    if fault is not None:
        fault_rows = [row for row in actions if row.get("status") == "fault_injected"]
        checks.append(
            _check(
                "fault_hit_once",
                fault.hits == 1
                and fault.hit_action_id is not None
                and [row.get("id") for row in fault_rows] == [fault.hit_action_id],
                "requested fault must be injected exactly once",
                fault=fault.kind,
                target=fault.target,
                hits=fault.hits,
                action_id=fault.hit_action_id,
            )
        )
    checks.extend(
        _final_truth_checks(
            manifest=manifest,
            before=before,
            after=after,
            fault=fault,
        )
    )
    fault_action = next(
        (
            row
            for row in actions
            if fault is not None and row.get("id") == fault.hit_action_id
        ),
        None,
    )
    fault_resource_id = fault_action.get("resource_id") if fault_action else None
    for resource in manifest.resources:
        baseline_values = _memory_series(baseline_samples, resource.gpu_uuid)
        recovery_values = _memory_series(recovery_samples, resource.gpu_uuid)
        checks.append(
            {
                "code": "baseline_memory_stable",
                "status": (
                    "passed"
                    if evaluate_stable_memory(baseline_values)["passed"]
                    else "blocked"
                ),
                "message": "baseline GPU memory must be stable",
                "details": {
                    "resource_id": resource.resource_id,
                    **evaluate_stable_memory(baseline_values),
                },
            }
        )
        checks.append(
            {
                "code": "recovery_memory_stable",
                "status": (
                    "passed"
                    if evaluate_stable_memory(recovery_values)["passed"]
                    else "blocked"
                ),
                "message": "post-action GPU memory must be stable",
                "details": {
                    "resource_id": resource.resource_id,
                    **evaluate_stable_memory(recovery_values),
                },
            }
        )
        redis_resource = (
            after.get("redis", {}).get("resources", {}).get(resource.resource_id, {})
        )
        snapshot = redis_resource.get("snapshot") or {}
        queue_transition_clean = bool(
            redis_resource.get("status") == "ready"
            and not snapshot.get("card_queue")
            and not snapshot.get("backend_queues")
            and snapshot.get("transition") is None
        )
        clean_runtime = queue_transition_clean and not snapshot.get("leases")
        if (
            fault is not None
            and resource.resource_id == fault_resource_id
            and fault.kind
            in {
                "response-lost-after-http",
                "cancel-after-grant",
            }
        ):
            target_backend_id = fault_action.get("backend_id") if fault_action else None
            target_generation = (
                fault_action.get("grant_generation") if fault_action else None
            )
            leases = snapshot.get("leases", [])
            target_leases = [
                lease
                for lease in leases
                if lease.get("backend_id") == target_backend_id
                and lease.get("generation") == target_generation
                and lease.get("state") in {"uncertain", "stale"}
            ]
            target_allocation = next(
                (
                    allocation
                    for allocation in snapshot.get("allocations", [])
                    if allocation.get("backend_id") == target_backend_id
                    and allocation.get("generation") == target_generation
                ),
                None,
            )
            conservative = bool(
                queue_transition_clean
                and target_backend_id
                and target_generation
                and target_allocation is not None
                and target_allocation.get("state") in {"resident", "unknown"}
                and target_allocation.get("budget_mb", 0) > 0
                and snapshot.get("committed_mb", 0)
                >= target_allocation.get("budget_mb", 0)
                and (target_leases or target_allocation.get("state") == "unknown")
                and len(target_leases) == len(leases)
            )
            checks.append(
                _check(
                    "fault_conservative_state",
                    conservative,
                    "uncertain transport must retain conservative allocation evidence",
                    resource_id=resource.resource_id,
                    backend_id=target_backend_id,
                    generation=target_generation,
                )
            )
        else:
            checks.append(
                _check(
                    "runtime_ephemera_clean",
                    clean_runtime,
                    "successful run must leave no lease, queue, or transition owner",
                    resource_id=resource.resource_id,
                )
            )

    if fault is not None:
        target_result = result_actions.get(fault.hit_action_id or "")
        result_shape_exact = bool(
            len(result_actions) == len(actions) == len(manifest_actions)
            and set(result_actions) == set(manifest_actions)
            and target_result is not None
            and target_result.get("status") == "fault_injected"
            and target_result.get("fault") == fault.kind
            and all(
                row.get("backend_id") == manifest_actions[action_id].backend_id
                and row.get("resource_id") == manifest_actions[action_id].resource_id
                and row.get("operation") == manifest_actions[action_id].operation
                and row.get("role") == manifest_actions[action_id].role
                and (
                    row.get("status") == "fault_injected"
                    if action_id == fault.hit_action_id
                    else row.get("status") == "passed"
                )
                for action_id, row in result_actions.items()
            )
        )
        target_backend_id = (
            str(target_result.get("backend_id")) if target_result is not None else None
        )
        target_resource_id = (
            str(target_result.get("resource_id")) if target_result is not None else None
        )
        target_generation = (
            target_result.get("grant_generation") if target_result is not None else None
        )
        fault_phase_exact = bool(
            target_result is not None
            and (
                (
                    fault.kind == "response-lost-after-http"
                    and target_generation is not None
                    and _parse_aware_datetime(
                        target_result.get("http_started_database_clock")
                    )
                    is not None
                    and _parse_aware_datetime(
                        target_result.get("http_finished_database_clock")
                    )
                    is not None
                    and isinstance(
                        target_result.get("http_started_monotonic_ms"), (int, float)
                    )
                    and isinstance(
                        target_result.get("http_finished_monotonic_ms"), (int, float)
                    )
                )
                or (
                    fault.kind == "cancel-after-grant"
                    and target_generation is not None
                    and _parse_aware_datetime(
                        target_result.get("http_started_database_clock")
                    )
                    is not None
                    and isinstance(
                        target_result.get("http_started_monotonic_ms"), (int, float)
                    )
                )
                or (
                    fault.kind == "health-timeout"
                    and target_generation is None
                    and target_result.get("http_started_database_clock") is None
                    and target_result.get("http_started_monotonic_ms") is None
                )
            )
        )
        abnormal_coordinates: list[tuple[str, str, Any]] = []
        target_allocation_present = False
        all_runtime_ephemera_scoped = True
        for resource_id, resource_row in (
            after.get("redis", {}).get("resources", {}).items()
        ):
            snapshot = resource_row.get("snapshot") or {}
            calculated_committed_mb = sum(
                allocation.get("budget_mb", 0)
                for allocation in snapshot.get("allocations", [])
                if allocation.get("state") != "unloaded"
            )
            if snapshot.get("committed_mb") != calculated_committed_mb:
                all_runtime_ephemera_scoped = False
            if snapshot.get("card_queue") or snapshot.get("backend_queues"):
                all_runtime_ephemera_scoped = False
            if snapshot.get("transition") is not None:
                all_runtime_ephemera_scoped = False
            for lease in snapshot.get("leases", []):
                if lease.get("state") in {"uncertain", "stale"}:
                    abnormal_coordinates.append(
                        (resource_id, lease.get("backend_id"), lease.get("generation"))
                    )
            for allocation in snapshot.get("allocations", []):
                if (
                    resource_id == target_resource_id
                    and allocation.get("backend_id") == target_backend_id
                    and allocation.get("generation") == target_generation
                    and allocation.get("state") in {"resident", "unknown"}
                ):
                    target_allocation_present = True
                if allocation.get("state") == "unknown":
                    abnormal_coordinates.append(
                        (
                            resource_id,
                            allocation.get("backend_id"),
                            allocation.get("generation"),
                        )
                    )
        expected_abnormal_coordinate = (
            target_resource_id,
            target_backend_id,
            target_generation,
        )
        transport_fault = fault.kind in {
            "response-lost-after-http",
            "cancel-after-grant",
        }
        abnormal_scope_exact = bool(
            all_runtime_ephemera_scoped
            and (
                bool(abnormal_coordinates)
                and target_generation is not None
                and all(
                    coordinate == expected_abnormal_coordinate
                    for coordinate in abnormal_coordinates
                )
                and target_allocation_present
                if transport_fault
                else not abnormal_coordinates
            )
        )
        checks.append(
            _check(
                "fault_scope_isolated",
                result_shape_exact and fault_phase_exact and abnormal_scope_exact,
                "fault evidence must affect only the exact target action and generation",
                target_action_id=fault.hit_action_id,
                target_backend_id=target_backend_id,
                target_resource_id=target_resource_id,
                target_generation=target_generation,
                abnormal_coordinates=abnormal_coordinates,
            )
        )

        peer_resource_ids = {
            resource.resource_id
            for resource in manifest.resources
            if resource.resource_id != target_resource_id
        }
        if peer_resource_ids:
            peer_action_ids = {
                action.id
                for action in manifest.actions
                if action.resource_id in peer_resource_ids
            }
            peer_backend_ids = {
                action.backend_id
                for action in manifest.actions
                if action.id in peer_action_ids
            }
            peer_http_passed = bool(peer_action_ids) and all(
                isinstance(result_actions.get(action_id, {}).get("http_status"), int)
                and 200 <= result_actions[action_id]["http_status"] < 300
                and isinstance(
                    result_actions[action_id].get("http_started_monotonic_ms"),
                    (int, float),
                )
                and isinstance(
                    result_actions[action_id].get("http_finished_monotonic_ms"),
                    (int, float),
                )
                and result_actions[action_id]["http_finished_monotonic_ms"]
                >= result_actions[action_id]["http_started_monotonic_ms"]
                for action_id in peer_action_ids
            )
            peer_truth_backends = {
                check.get("details", {}).get("backend_id")
                for check in checks
                if check.get("code") == "final_backend_truth_exact"
                and check.get("status") == "passed"
                and isinstance(check.get("details"), Mapping)
            }
            peer_resident_backends = {
                allocation.get("backend_id")
                for resource_id in peer_resource_ids
                for allocation in (
                    after.get("redis", {})
                    .get("resources", {})
                    .get(resource_id, {})
                    .get("snapshot", {})
                    .get("allocations", [])
                )
                if allocation.get("state") == "resident"
            }
            peer_allocation_backend_ids = {
                allocation.get("backend_id")
                for resource_id in peer_resource_ids
                for allocation in (
                    after.get("redis", {})
                    .get("resources", {})
                    .get(resource_id, {})
                    .get("snapshot", {})
                    .get("allocations", [])
                )
            }
            peer_nonaction_allocations_unchanged = True
            for resource_id in peer_resource_ids:
                before_allocations = {
                    allocation.get("backend_id"): allocation
                    for allocation in (
                        before.get("redis", {})
                        .get("resources", {})
                        .get(resource_id, {})
                        .get("snapshot", {})
                        .get("allocations", [])
                    )
                }
                after_allocations = {
                    allocation.get("backend_id"): allocation
                    for allocation in (
                        after.get("redis", {})
                        .get("resources", {})
                        .get(resource_id, {})
                        .get("snapshot", {})
                        .get("allocations", [])
                    )
                }
                nonaction_backend_ids = (
                    set(before_allocations) | set(after_allocations)
                ) - peer_backend_ids
                if any(
                    before_allocations.get(backend_id)
                    != after_allocations.get(backend_id)
                    for backend_id in nonaction_backend_ids
                ):
                    peer_nonaction_allocations_unchanged = False
                    break
            peer_runtime_clean = all(
                not (
                    (
                        snapshot := (
                            after.get("redis", {})
                            .get("resources", {})
                            .get(resource_id, {})
                            .get("snapshot", {})
                        )
                    ).get("leases")
                    or snapshot.get("card_queue")
                    or snapshot.get("backend_queues")
                    or snapshot.get("transition") is not None
                )
                for resource_id in peer_resource_ids
            )
            checks.append(
                _check(
                    "fault_peer_resource_isolation",
                    peer_http_passed
                    and peer_backend_ids.issubset(peer_truth_backends)
                    and peer_backend_ids.issubset(peer_resident_backends)
                    and peer_allocation_backend_ids.issubset(peer_truth_backends)
                    and peer_nonaction_allocations_unchanged
                    and peer_runtime_clean,
                    "a target-card fault must not contaminate peer-card execution or truth",
                    peer_resource_ids=sorted(peer_resource_ids),
                    peer_action_ids=sorted(peer_action_ids),
                    peer_backend_ids=sorted(peer_backend_ids),
                    peer_allocation_backend_ids=sorted(peer_allocation_backend_ids),
                )
            )
    if fault is not None and fault.kind == "health-timeout":
        target_resource_id = (
            fault_action.get("resource_id") if fault_action is not None else None
        )
        before_allocation = (
            _snapshot_allocation(
                before,
                resource_id=target_resource_id,
                backend_id=fault.target,
            )
            if target_resource_id is not None
            else None
        )
        after_allocation = (
            _snapshot_allocation(
                after,
                resource_id=target_resource_id,
                backend_id=fault.target,
            )
            if target_resource_id is not None
            else None
        )
        checks.append(
            _check(
                "health_timeout_preserves_victim",
                bool(
                    fault_action
                    and fault_action.get("grant_generation") is None
                    and before_allocation
                    and after_allocation
                    and before_allocation.get("state") == "resident"
                    and before_allocation == after_allocation
                ),
                "health timeout before admission must preserve the exact Resident victim",
                backend_id=fault.target,
                resource_id=target_resource_id,
            )
        )
    if fault is None and manifest.scenario == "dual-card":
        execution_samples = [*during_samples, after]
        overlaps = [
            action_overlap_ms(
                {
                    "started_monotonic_ms": left.get("http_started_monotonic_ms", 0),
                    "finished_monotonic_ms": left.get("http_finished_monotonic_ms", 0),
                },
                {
                    "started_monotonic_ms": right.get("http_started_monotonic_ms", 0),
                    "finished_monotonic_ms": right.get("http_finished_monotonic_ms", 0),
                },
            )
            for index, left in enumerate(actions)
            for right in actions[index + 1 :]
            if left["resource_id"] != right["resource_id"]
        ]
        best_overlap = max(overlaps, default=0.0)
        checks.append(
            _check(
                "dual_card_parallel_overlap",
                best_overlap >= MIN_PARALLEL_OVERLAP_MS,
                "actions on distinct physical resources must overlap",
                overlap_ms=best_overlap,
            )
        )
        execution_by_resource = {
            resource.resource_id: _resource_gpu_execution_observed(
                execution_samples,
                resource_id=resource.resource_id,
                backend_ids={
                    action.backend_id
                    for action in manifest.actions
                    if action.resource_id == resource.resource_id
                },
            )
            for resource in manifest.resources
        }
        final_resident_resources = {
            resource_id
            for resource_id, resource in after.get("redis", {})
            .get("resources", {})
            .items()
            if any(
                allocation.get("backend_id")
                in {
                    action.backend_id
                    for action in manifest.actions
                    if action.resource_id == resource_id
                }
                and allocation.get("state") == "resident"
                for allocation in (resource.get("snapshot") or {}).get(
                    "allocations", []
                )
            )
        }
        checks.append(
            _check(
                "multi_resource_gpu_execution",
                all(execution_by_resource.values())
                and final_resident_resources
                == {resource.resource_id for resource in manifest.resources},
                "every local physical resource must show Resident GPU execution",
                execution_by_resource=execution_by_resource,
                final_resident_resources=sorted(final_resident_resources),
            )
        )
    elif fault is None and manifest.scenario == "cross-host":
        resource = manifest.resources[0]
        backend_ids = {action.backend_id for action in manifest.actions}
        final_snapshot = (
            after.get("redis", {})
            .get("resources", {})
            .get(resource.resource_id, {})
            .get("snapshot", {})
        )
        passed = _resource_gpu_execution_observed(
            [*during_samples, after],
            resource_id=resource.resource_id,
            backend_ids=backend_ids,
        ) and any(
            allocation.get("backend_id") in backend_ids
            and allocation.get("state") == "resident"
            for allocation in final_snapshot.get("allocations", [])
        )
        checks.append(
            _check(
                "cross_host_local_gpu_execution",
                passed,
                "cross-host evidence must show local Resident GPU execution",
                resource_id=resource.resource_id,
            )
        )
    elif fault is None and manifest.scenario == "single-card-co-residency":
        resource_id = manifest.resources[0].resource_id
        snapshot = (
            after.get("redis", {})
            .get("resources", {})
            .get(resource_id, {})
            .get("snapshot", {})
        )
        resident_backend_ids = {
            allocation["backend_id"]
            for allocation in snapshot.get("allocations", [])
            if allocation.get("state") == "resident"
        }
        expected_backend_ids = {action.backend_id for action in manifest.actions}
        checks.append(
            _check(
                "single_card_co_residency",
                len(expected_backend_ids) >= 2
                and expected_backend_ids.issubset(resident_backend_ids),
                "one physical card must retain all requested backend allocations",
                resource_id=resource_id,
                expected_backend_ids=sorted(expected_backend_ids),
                resident_backend_ids=sorted(resident_backend_ids),
            )
        )
    elif fault is None and manifest.scenario == "single-card-eviction":
        resource_id = manifest.resources[0].resource_id
        snapshot = (
            after.get("redis", {})
            .get("resources", {})
            .get(resource_id, {})
            .get("snapshot", {})
        )
        states = {
            allocation["backend_id"]: allocation.get("state")
            for allocation in snapshot.get("allocations", [])
        }
        requester_ids = {
            action.backend_id
            for action in manifest.actions
            if action.role == "requester"
        }
        victim_ids = {
            action.backend_id for action in manifest.actions if action.role == "victim"
        }
        passed = (
            bool(requester_ids and victim_ids)
            and all(
                states.get(backend_id) == "resident" for backend_id in requester_ids
            )
            and all(states.get(backend_id) == "unloaded" for backend_id in victim_ids)
        )
        checks.append(
            _check(
                "single_card_eviction",
                passed,
                "requester must be Resident and every declared victim Unloaded",
                resource_id=resource_id,
                allocation_states=states,
            )
        )
        victim_state_evidence = {
            backend_id: sorted(
                _during_allocation_states(
                    during_samples,
                    resource_id=resource_id,
                    backend_id=backend_id,
                )
                | {
                    str(before_state)
                    for before_state in (
                        (
                            _snapshot_allocation(
                                before,
                                resource_id=resource_id,
                                backend_id=backend_id,
                            )
                            or {}
                        ).get("state"),
                    )
                    if before_state is not None
                }
            )
            for backend_id in victim_ids
        }
        victim_transition_seen = bool(victim_state_evidence) and all(
            "resident" in states
            and bool({"draining", "unloading"}.intersection(states))
            for states in victim_state_evidence.values()
        )
        transition_evidence = {
            victim_id: [
                transition
                for sample in during_samples
                if (
                    transition := (
                        sample.get("redis", {})
                        .get("resources", {})
                        .get(resource_id, {})
                        .get("snapshot", {})
                        .get("transition")
                    )
                )
                and transition.get("operation") == "evict"
                and transition.get("backend_id") == victim_id
                and transition.get("requester_backend_id") in requester_ids
                and transition.get("eviction_branch") == "unload"
            ]
            for victim_id in victim_ids
        }
        exact_transition_seen = bool(transition_evidence) and all(
            transitions for transitions in transition_evidence.values()
        )
        checks.append(
            _check(
                "single_card_victim_transition_observed",
                victim_transition_seen and exact_transition_seen,
                "every victim must show exact requester-bound eviction and frozen unload branch",
                resource_id=resource_id,
                victim_states=victim_state_evidence,
                transition_evidence=transition_evidence,
            )
        )
    elif fault is None and manifest.scenario == "single-card-capacity-rejection":
        resource_id = manifest.resources[0].resource_id
        before_snapshot = (
            before.get("redis", {})
            .get("resources", {})
            .get(resource_id, {})
            .get("snapshot", {})
        )
        after_snapshot = (
            after.get("redis", {})
            .get("resources", {})
            .get(resource_id, {})
            .get("snapshot", {})
        )
        action = manifest.actions[0]
        result = result_actions.get(action.id, {})
        allocations_unchanged = before_snapshot.get(
            "allocations"
        ) == after_snapshot.get("allocations") and before_snapshot.get(
            "committed_mb"
        ) == after_snapshot.get("committed_mb")
        checks.append(
            _check(
                "single_card_capacity_rejected_before_http",
                bool(
                    result.get("status") == "passed"
                    and result.get("expected_error_code")
                    == result.get("error_code")
                    == "gpu_capacity_unavailable"
                    and result.get("error_http_status") == 503
                    and result.get("grant_generation") is None
                    and result.get("http_status") is None
                    and result.get("http_started_monotonic_ms") is None
                    and allocations_unchanged
                ),
                "capacity rejection must occur before backend HTTP and preserve allocations",
                resource_id=resource_id,
                backend_id=action.backend_id,
                allocations_unchanged=allocations_unchanged,
            )
        )
    return checks


async def run_validation(
    *,
    manifest: ValidationManifest,
    manifest_sha256: str,
    run_id: str,
    confirm_run_id: str,
    fault_kind: str | None,
    fault_target: str | None,
    session_factory,
    config: Settings,
) -> dict[str, Any]:
    import asyncpg
    from aap_inference.gpu.dispatch import build_gpu_dispatch_context_factory
    from aap_inference.gpu.endpoint_source import GPUOwnedBackendEndpointSource
    from aap_inference.gpu.native import GPUAuthorityPoolData
    from aap_inference.gpu_health import PersistedGPUHealthRefresher
    from aap_inference.owner_transport import BackendTransportGPUControlAdapter
    from aap_inference.routing_runtime import RoutingRuntime
    from app.services.inference_limits import capability_limits_from_settings

    _validate_run_safety(
        environment=config.environment,
        run_id=run_id,
        confirm_run_id=confirm_run_id,
    )
    if (fault_kind is None) != (fault_target is None):
        raise ValueError("--fault and --fault-target must be provided together")
    if manifest.scenario == "single-card-capacity-rejection" and fault_kind:
        raise ValueError("capacity-rejection does not allow fault injection")
    fault = (
        FaultController(kind=fault_kind, target=fault_target)  # type: ignore[arg-type]
        if fault_kind and fault_target
        else None
    )
    store = GPUArbiterStore.from_url(config.redis_url)
    owner_stack = AsyncExitStack()
    started_at = _utc_now()
    evidence_manifest = _evidence_manifest(manifest)
    evidence_manifest_sha256 = _sha256_json(evidence_manifest)
    try:
        preflight, endpoints = await collect_preflight(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            session_factory=session_factory,
            config=config,
            store=store,
        )
        if not _preflight_allows_runtime_proof_refresh(preflight):
            return {
                "schema": EVIDENCE_SCHEMA,
                "command": "run",
                "status": "blocked",
                "run_id": run_id,
                "cohort_id": manifest.cohort_id,
                "node_id": manifest.node_id,
                "scenario": manifest.scenario,
                "resources": _manifest_resources(manifest),
                "started_at": started_at,
                "finished_at": _utc_now(),
                "manifest_sha256": manifest_sha256,
                "evidence_manifest": evidence_manifest,
                "evidence_manifest_sha256": evidence_manifest_sha256,
                "thresholds": _thresholds(),
                "threshold_applicability": _threshold_applicability(),
                "checks": preflight["checks"],
                "preflight": preflight,
                "actions": [],
                "snapshots": {},
                "faults": [],
                "cleanup": {"performed": False, "reason": "preflight_blocked"},
            }

        runtime_proof_checks = await refresh_runtime_proofs(
            endpoints,
        )
        if not all(check["status"] == "passed" for check in runtime_proof_checks):
            return {
                "schema": EVIDENCE_SCHEMA,
                "command": "run",
                "status": "blocked",
                "run_id": run_id,
                "cohort_id": manifest.cohort_id,
                "node_id": manifest.node_id,
                "scenario": manifest.scenario,
                "resources": _manifest_resources(manifest),
                "started_at": started_at,
                "finished_at": _utc_now(),
                "manifest_sha256": manifest_sha256,
                "evidence_manifest": evidence_manifest,
                "evidence_manifest_sha256": evidence_manifest_sha256,
                "thresholds": _thresholds(),
                "threshold_applicability": _threshold_applicability(),
                "checks": [*preflight["checks"], *runtime_proof_checks],
                "preflight": preflight,
                "actions": [],
                "snapshots": {},
                "faults": [],
                "cleanup": {
                    "performed": False,
                    "reason": "runtime_proof_refresh_blocked",
                },
            }

        preflight, endpoints = await collect_preflight(
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            session_factory=session_factory,
            config=config,
            store=store,
        )
        if preflight["status"] != "passed":
            return {
                "schema": EVIDENCE_SCHEMA,
                "command": "run",
                "status": "blocked",
                "run_id": run_id,
                "cohort_id": manifest.cohort_id,
                "node_id": manifest.node_id,
                "scenario": manifest.scenario,
                "resources": _manifest_resources(manifest),
                "started_at": started_at,
                "finished_at": _utc_now(),
                "manifest_sha256": manifest_sha256,
                "evidence_manifest": evidence_manifest,
                "evidence_manifest_sha256": evidence_manifest_sha256,
                "thresholds": _thresholds(),
                "threshold_applicability": _threshold_applicability(),
                "checks": [*runtime_proof_checks, *preflight["checks"]],
                "preflight": preflight,
                "actions": [],
                "snapshots": {},
                "faults": [],
                "cleanup": {
                    "performed": False,
                    "reason": "post_refresh_preflight_blocked",
                },
            }

        routing = RoutingRuntime(_owner_database_url("INFERENCE_ROUTING_DATABASE_URL"))
        owner_stack.push_async_callback(routing.close)

        async def owner_pool(name: str):
            pool = await asyncpg.create_pool(
                _owner_database_url(name),
                min_size=0,
                max_size=8,
                timeout=5,
                command_timeout=30,
            )
            owner_stack.push_async_callback(pool.close)
            return pool

        authority = GPUAuthorityPoolData(await owner_pool("INFERENCE_GPU_DATABASE_URL"))
        endpoint_source = GPUOwnedBackendEndpointSource(
            {
                purpose: await owner_pool(
                    f"INFERENCE_GPU_ENDPOINT_{purpose.upper()}_DATABASE_URL"
                )
                for purpose in ("health_observation", "gpu_control")
            }
        )
        persisted_health = PersistedGPUHealthRefresher(
            _owner_database_url("INFERENCE_HEALTH_DATA_DATABASE_URL"),
            options=config,
            capability_limits_factory=capability_limits_from_settings,
        )

        async def refresh_health(backend_id: uuid.UUID, challenge: str) -> bool:
            if fault is not None and fault.hit_health(backend_id):
                raise TimeoutError("injected health-timeout")
            return await persisted_health(backend_id, challenge)

        @asynccontextmanager
        async def store_factory():
            owned_store = GPUArbiterStore.from_url(config.redis_url)
            try:
                yield owned_store
            finally:
                await owned_store.aclose()

        owner_config = _owner_settings(config)
        dispatch_factory = build_gpu_dispatch_context_factory(
            authority,
            endpoint_source,
            BackendTransportGPUControlAdapter(routing),
            config=owner_config,
            store_factory=store_factory,
            signer_factory=lambda: GPUAdmissionTokenSigner.from_settings(owner_config),
            health_refresher=refresh_health,
        )
        baseline_samples = await _gpu_samples()
        before_runtime = await _runtime_sample(
            store=store,
            manifest=manifest,
            endpoints=endpoints,
        )
        before_database, _ = await collect_database_snapshot(session_factory, manifest)
        before = {
            "database": before_database,
            **before_runtime,
        }
        action_started_database_clock = await collect_database_clock(session_factory)
        action_task = asyncio.gather(
            *(
                _run_action(
                    action,
                    endpoint=endpoints[action.backend_id],
                    dispatch_factory=dispatch_factory,
                    database_clock=lambda: collect_database_clock(session_factory),
                    fault=fault,
                )
                for action in manifest.actions
            )
        )
        try:
            during: list[dict[str, Any]] = []
            while not action_task.done():
                during.append(
                    await _runtime_sample(
                        store=store,
                        manifest=manifest,
                        endpoints=endpoints,
                    )
                )
                if not action_task.done():
                    await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
            actions = await action_task
        finally:
            if not action_task.done():
                action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
        action_finished_database_clock = await collect_database_clock(session_factory)
        after_runtime = await _runtime_sample(
            store=store,
            manifest=manifest,
            endpoints=endpoints,
        )
        after_database, _ = await collect_database_snapshot(session_factory, manifest)
        after = {
            "database": after_database,
            **after_runtime,
        }
        recovery_samples = await _gpu_samples()
        checks = [
            *runtime_proof_checks,
            *evaluate_run(
                manifest=manifest,
                actions=actions,
                before=before,
                after=after,
                baseline_samples=baseline_samples,
                during_samples=during,
                recovery_samples=recovery_samples,
                fault=fault,
            ),
        ]
        passed = all(check["status"] == "passed" for check in checks)
        return {
            "schema": EVIDENCE_SCHEMA,
            "command": "run",
            "status": "passed" if passed else "failed",
            "run_id": run_id,
            "cohort_id": manifest.cohort_id,
            "node_id": manifest.node_id,
            "scenario": manifest.scenario,
            "resources": _manifest_resources(manifest),
            "started_at": started_at,
            "finished_at": _utc_now(),
            "manifest_sha256": manifest_sha256,
            "evidence_manifest": evidence_manifest,
            "evidence_manifest_sha256": evidence_manifest_sha256,
            "thresholds": _thresholds(),
            "threshold_applicability": _threshold_applicability(),
            "checks": checks,
            "actions": actions,
            "snapshots": {
                "baseline_gpu": baseline_samples,
                "action_window": {
                    "started_database_clock": action_started_database_clock,
                    "finished_database_clock": action_finished_database_clock,
                },
                "before": before,
                "during": during,
                "after": after,
                "recovery_gpu": recovery_samples,
            },
            "faults": (
                [
                    {
                        "kind": fault.kind,
                        "target": fault.target,
                        "hits": fault.hits,
                        "action_id": fault.hit_action_id,
                    }
                ]
                if fault
                else []
            ),
            "cleanup": {
                "performed": True,
                "scope": "runner-owned HTTP clients and samplers only",
                "redis_namespace_deleted": False,
            },
        }
    finally:
        try:
            await owner_stack.aclose()
        finally:
            await store.aclose()


_PRIMARY_CHECKS = frozenset(
    {
        "run_runtime_proof_refreshed",
        "actions_completed",
        "run_topology_stable",
        "run_fence_monotonic",
        "final_membership_domain_exact",
        "final_backend_truth_exact",
        "baseline_memory_stable",
        "recovery_memory_stable",
        "runtime_ephemera_clean",
    }
)
_SCENARIO_PRIMARY_CHECKS = {
    "single-card-co-residency": frozenset({"single_card_co_residency"}),
    "single-card-eviction": frozenset(
        {"single_card_eviction", "single_card_victim_transition_observed"}
    ),
    "single-card-capacity-rejection": frozenset(
        {"single_card_capacity_rejected_before_http"}
    ),
    "dual-card": frozenset(
        {"dual_card_parallel_overlap", "multi_resource_gpu_execution"}
    ),
    "cross-host": frozenset({"cross_host_local_gpu_execution"}),
}


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _conservative_database_http_window(
    action: Mapping[str, Any],
) -> tuple[datetime, datetime] | None:
    started = _parse_aware_datetime(action.get("http_started_database_clock"))
    finished = _parse_aware_datetime(action.get("http_finished_database_clock"))
    started_rtt_ms = action.get("http_started_database_probe_rtt_ms")
    finished_rtt_ms = action.get("http_finished_database_probe_rtt_ms")
    if (
        started is None
        or finished is None
        or not isinstance(started_rtt_ms, (int, float))
        or isinstance(started_rtt_ms, bool)
        or not math.isfinite(float(started_rtt_ms))
        or float(started_rtt_ms) < 0
        or not isinstance(finished_rtt_ms, (int, float))
        or isinstance(finished_rtt_ms, bool)
        or not math.isfinite(float(finished_rtt_ms))
        or float(finished_rtt_ms) < 0
    ):
        return None
    conservative_started = started + timedelta(milliseconds=float(started_rtt_ms))
    conservative_finished = finished - timedelta(milliseconds=float(finished_rtt_ms))
    if conservative_finished < conservative_started:
        return None
    return conservative_started, conservative_finished


def _action_http_window_valid(
    action: Mapping[str, Any],
    *,
    require_database_window: bool,
) -> bool:
    started = action.get("http_started_monotonic_ms")
    finished = action.get("http_finished_monotonic_ms")
    status = action.get("http_status")
    database_window = _conservative_database_http_window(action)
    return bool(
        isinstance(status, int)
        and not isinstance(status, bool)
        and 200 <= status < 300
        and isinstance(started, (int, float))
        and not isinstance(started, bool)
        and math.isfinite(float(started))
        and isinstance(finished, (int, float))
        and not isinstance(finished, bool)
        and math.isfinite(float(finished))
        and float(finished) >= float(started)
        and (not require_database_window or database_window is not None)
    )


def _primary_action_valid(
    spec: ActionSpec,
    action: Mapping[str, Any],
    *,
    require_database_window: bool,
) -> bool:
    if action.get("status") != "passed":
        return False
    if spec.expected_error_code is None:
        return _action_http_window_valid(
            action,
            require_database_window=require_database_window,
        )
    started = action.get("started_monotonic_ms")
    finished = action.get("finished_monotonic_ms")
    return bool(
        action.get("expected_error_code")
        == action.get("error_code")
        == spec.expected_error_code
        and action.get("error_http_status") == 503
        and not any(
            key in action
            for key in (
                "grant_generation",
                "http_status",
                "http_started_monotonic_ms",
                "http_finished_monotonic_ms",
            )
        )
        and isinstance(started, (int, float))
        and not isinstance(started, bool)
        and math.isfinite(float(started))
        and isinstance(finished, (int, float))
        and not isinstance(finished, bool)
        and math.isfinite(float(finished))
        and float(finished) >= float(started)
    )


def _primary_report_valid(report: Mapping[str, Any]) -> bool:
    if not isinstance(report, Mapping):
        return False
    try:
        evidence_manifest_payload = report.get("evidence_manifest")
        if not isinstance(evidence_manifest_payload, Mapping):
            return False
        manifest = ValidationManifest.model_validate(
            evidence_manifest_payload, strict=True
        )
        actions = report.get("actions")
        checks = report.get("checks")
        snapshots = report.get("snapshots")
        started_at = _parse_aware_datetime(report.get("started_at"))
        finished_at = _parse_aware_datetime(report.get("finished_at"))
        action_rows = {
            action.get("id"): action
            for action in actions
            if isinstance(actions, list)
            and isinstance(action, Mapping)
            and isinstance(action.get("id"), str)
        }
        action_specs = {action.id: action for action in manifest.actions}
        if (
            report.get("schema") != EVIDENCE_SCHEMA
            or report.get("command") != "run"
            or report.get("status") != "passed"
            or report.get("thresholds") != _thresholds()
            or report.get("threshold_applicability") != _threshold_applicability()
            or report.get("faults") != []
            or report.get("scenario") != manifest.scenario
            or report.get("cohort_id") != manifest.cohort_id
            or report.get("node_id") != manifest.node_id
            or report.get("resources") != _manifest_resources(manifest)
            or report.get("evidence_manifest_sha256")
            != _sha256_json(evidence_manifest_payload)
            or not isinstance(report.get("manifest_sha256"), str)
            or _SHA256_RE.fullmatch(report["manifest_sha256"]) is None
            or not isinstance(report.get("run_id"), str)
            or _RUN_ID_RE.fullmatch(report["run_id"]) is None
            or started_at is None
            or finished_at is None
            or finished_at < started_at
            or not isinstance(actions, list)
            or not actions
            or len(action_rows) != len(actions)
            or set(action_rows) != set(action_specs)
            or not all(
                _primary_action_valid(
                    action_specs[action_id],
                    action,
                    require_database_window=manifest.scenario == "cross-host",
                )
                for action_id, action in action_rows.items()
            )
            or not isinstance(checks, list)
            or not checks
            or not isinstance(snapshots, Mapping)
            or report.get("cleanup", {}).get("performed") is not True
        ):
            return False
        recomputed_checks = evaluate_run(
            manifest=manifest,
            actions=actions,
            before=snapshots["before"],
            after=snapshots["after"],
            baseline_samples=snapshots["baseline_gpu"],
            during_samples=snapshots["during"],
            recovery_samples=snapshots["recovery_gpu"],
            fault=None,
        )
        runtime_backend_ids = sorted(
            row["backend_id"] for row in snapshots["before"]["database"]["registries"]
        )
        if len(runtime_backend_ids) != len(set(runtime_backend_ids)) or any(
            str(uuid.UUID(backend_id)) != backend_id
            for backend_id in runtime_backend_ids
        ):
            return False
        runtime_proof_checks = [
            _check(
                "run_runtime_proof_refreshed",
                True,
                "run must persist fresh challenge-bound health before dispatch",
                backend_id=backend_id,
            )
            for backend_id in runtime_backend_ids
        ]
    except Exception:  # noqa: BLE001 - malformed evidence must verify as failed
        return False
    if any(check.get("status") != "passed" for check in recomputed_checks):
        return False
    required_codes = _PRIMARY_CHECKS | _SCENARIO_PRIMARY_CHECKS[manifest.scenario]
    expected_checks = [*runtime_proof_checks, *recomputed_checks]
    recomputed_codes = {check.get("code") for check in expected_checks}
    return bool(
        required_codes.issubset(recomputed_codes)
        and _canonical_json(checks) == _canonical_json(expected_checks)
    )


def verify_evidence(
    reports: Sequence[Mapping[str, Any]],
    *,
    scenario: Literal[
        "single-card-co-residency",
        "single-card-eviction",
        "single-card-capacity-rejection",
        "dual-card",
        "cross-host",
    ],
) -> dict[str, Any]:
    input_reports = tuple(
        report if isinstance(report, Mapping) else {} for report in reports
    )
    primary_valid = bool(input_reports) and all(
        _primary_report_valid(report) for report in input_reports
    )
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "evidence_schema",
            bool(input_reports)
            and all(
                report.get("schema") == EVIDENCE_SCHEMA for report in input_reports
            ),
            "all evidence files must use the frozen schema",
        )
    )
    checks.append(
        _check(
            "primary_evidence_shape",
            primary_valid,
            "every input must be a fault-free run with frozen thresholds and complete proofs",
        )
    )
    cohorts = {report.get("cohort_id") for report in input_reports}
    checks.append(
        _check(
            "cohort_exact",
            len(cohorts) == 1 and None not in cohorts,
            "all evidence files must belong to one cohort",
            cohorts=sorted(str(value) for value in cohorts),
        )
    )
    checks.append(
        _check(
            "runs_passed",
            bool(input_reports)
            and all(report.get("status") == "passed" for report in input_reports),
            "all input runs must pass locally",
        )
    )
    reports = input_reports if primary_valid else ()
    if scenario == "cross-host":
        nodes = {report.get("node_id") for report in reports}
        resource_rows = [
            resource
            for report in reports
            for resource in report.get("resources", [])
            if isinstance(resource, Mapping)
        ]
        resource_ids = {resource.get("resource_id") for resource in resource_rows}
        gpu_indexes: list[int] = []
        for report in reports:
            by_uuid = {
                gpu.get("uuid"): gpu.get("index")
                for gpu in (
                    report.get("snapshots", {})
                    .get("after", {})
                    .get("nvidia_smi", {})
                    .get("gpus", [])
                )
                if isinstance(gpu, Mapping)
            }
            selected = report.get("resources", [])
            if (
                isinstance(selected, list)
                and len(selected) == 1
                and isinstance(selected[0], Mapping)
                and selected[0].get("gpu_uuid") in by_uuid
                and isinstance(by_uuid[selected[0]["gpu_uuid"]], int)
                and not isinstance(by_uuid[selected[0]["gpu_uuid"]], bool)
            ):
                gpu_indexes.append(by_uuid[selected[0]["gpu_uuid"]])
        checks.append(
            _check(
                "cross_host_identity",
                len(reports) == 2
                and all(report.get("scenario") == "cross-host" for report in reports)
                and len(nodes) == len(reports)
                and None not in nodes
                and len(resource_rows) == len(reports)
                and len(resource_ids) == len(reports)
                and None not in resource_ids
                and len(gpu_indexes) == len(reports)
                and len(set(gpu_indexes)) == 1,
                "cross-host evidence requires distinct nodes/resources on the same GPU index",
                nodes=sorted(str(value) for value in nodes),
                resources=sorted(str(value) for value in resource_ids),
                gpu_indexes=gpu_indexes,
            )
        )
        intervals: list[tuple[str, datetime, datetime]] = []
        for report in reports:
            for action in report.get("actions", []):
                if not isinstance(action, Mapping):
                    continue
                database_window = _conservative_database_http_window(action)
                if action.get("status") == "passed" and database_window is not None:
                    intervals.append(
                        (
                            str(report.get("node_id")),
                            database_window[0],
                            database_window[1],
                        )
                    )
        overlaps = [
            max(
                0.0,
                (min(left[2], right[2]) - max(left[1], right[1])).total_seconds()
                * 1000,
            )
            for index, left in enumerate(intervals)
            for right in intervals[index + 1 :]
            if left[0] != right[0]
        ]
        best_overlap = max(overlaps, default=0.0)
        checks.append(
            _check(
                "cross_host_parallel_overlap",
                {interval[0] for interval in intervals}
                == {str(report.get("node_id")) for report in reports}
                and best_overlap >= MIN_PARALLEL_OVERLAP_MS,
                "cross-host HTTP execution must overlap on the shared database clock",
                overlap_ms=best_overlap,
            )
        )
    elif scenario == "dual-card":
        report = reports[0] if len(reports) == 1 else {}
        resources = report.get("resources", [])
        resource_ids = {
            resource.get("resource_id")
            for resource in resources
            if isinstance(resource, Mapping)
        }
        actions = [
            action
            for action in report.get("actions", [])
            if isinstance(action, Mapping)
            and _action_http_window_valid(
                action,
                require_database_window=False,
            )
        ]
        overlaps = [
            max(
                0.0,
                min(
                    float(left["http_finished_monotonic_ms"]),
                    float(right["http_finished_monotonic_ms"]),
                )
                - max(
                    float(left["http_started_monotonic_ms"]),
                    float(right["http_started_monotonic_ms"]),
                ),
            )
            for index, left in enumerate(actions)
            for right in actions[index + 1 :]
            if left.get("resource_id") != right.get("resource_id")
        ]
        observed_resources = {action.get("resource_id") for action in actions}
        best_overlap = max(overlaps, default=0.0)
        checks.append(
            _check(
                "dual_card_evidence",
                len(reports) == 1
                and report.get("scenario") == "dual-card"
                and len(resources) >= 2
                and len(resource_ids) == len(resources)
                and None not in resource_ids
                and resource_ids == observed_resources
                and best_overlap >= MIN_PARALLEL_OVERLAP_MS,
                "one local run must recompute exact dual-card HTTP overlap",
                overlap_ms=best_overlap,
                resources=sorted(str(value) for value in resource_ids),
            )
        )
    else:
        checks.append(
            _check(
                "single_card_evidence",
                len(reports) == 1 and reports[0].get("scenario") == scenario,
                "single-card verification accepts one exact local subscenario",
                acceptance_scope="evidence-consistency-only",
            )
        )
    passed = all(check["status"] == "passed" for check in checks)
    return {
        "schema": EVIDENCE_SCHEMA,
        "command": "verify",
        "status": "passed" if passed else "failed",
        "scenario": scenario,
        "observed_at": _utc_now(),
        "checks": checks,
        "input_sha256": [_sha256_json(report) for report in input_reports],
    }


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    encoded = json.dumps(
        _json_safe(report), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(encoded + "\n", encoding="utf-8")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    print(encoded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate cross-backend GPU memory arbitration evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight", help="Run read-only preflight")
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    run = subparsers.add_parser("run", help="Run non-production acceptance actions")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--confirm-run-id", required=True)
    run.add_argument(
        "--fault",
        choices=(
            "response-lost-after-http",
            "cancel-after-grant",
            "health-timeout",
        ),
    )
    run.add_argument("--fault-target")
    run.add_argument("--output", type=Path)
    verify = subparsers.add_parser("verify", help="Verify one or more evidence files")
    verify.add_argument("evidence", type=Path, nargs="+")
    verify.add_argument(
        "--scenario",
        choices=(
            "single-card-co-residency",
            "single-card-eviction",
            "single-card-capacity-rejection",
            "dual-card",
            "cross-host",
        ),
        required=True,
    )
    verify.add_argument("--output", type=Path)
    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_engine = None
    try:
        if args.command == "verify":
            reports = [json.loads(path.read_bytes()) for path in args.evidence]
            report = verify_evidence(reports, scenario=args.scenario)
        else:
            manifest, _, manifest_sha256 = load_manifest(args.manifest)
            if args.command == "run":
                _validate_run_safety(
                    environment=settings.environment,
                    run_id=args.run_id,
                    confirm_run_id=args.confirm_run_id,
                )
            from app.db.base import (  # noqa: PLC0415
                async_session,
                engine as active_database_engine,
            )

            database_engine = active_database_engine
            if args.command == "preflight":
                store = GPUArbiterStore.from_url(settings.redis_url)
                try:
                    report, _ = await collect_preflight(
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                        session_factory=async_session,
                        config=settings,
                        store=store,
                    )
                finally:
                    await store.aclose()
            else:
                report = await run_validation(
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                    run_id=args.run_id,
                    confirm_run_id=args.confirm_run_id,
                    fault_kind=args.fault,
                    fault_target=args.fault_target,
                    session_factory=async_session,
                    config=settings,
                )
    except Exception as exc:  # noqa: BLE001 - CLI must emit one blocked JSON report
        report = {
            "schema": EVIDENCE_SCHEMA,
            "command": args.command,
            "status": "blocked",
            "observed_at": _utc_now(),
            "checks": [
                _check(
                    "runner_safety",
                    False,
                    "runner safety or configuration check failed",
                    error=_safe_error(exc),
                )
            ],
        }
    finally:
        if database_engine is not None:
            await database_engine.dispose()

    _write_report(report, args.output)
    if report["status"] == "passed":
        return 0
    return 2 if report["status"] == "blocked" else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
