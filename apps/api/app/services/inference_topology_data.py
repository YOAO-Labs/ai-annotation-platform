"""Borrowed AsyncSession adapter for the fixed Inference topology Native API."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_inference.plugin_installation import validate_installation_command
from aap_inference.topology import (
    RegistryDeletion,
    RegistryTopologySnapshot,
    RepairModelLockStatus,
)
from aap_inference_contract.topology import (
    PoolFacts,
    PoolMemberFacts,
    ProjectAssociationFacts,
    ProjectBackendFacts,
    ProjectPoolFacts,
    RegistryName,
    RegistryPublicFacts,
    RegistryReference,
    to_record,
)
from aap_inference_contract.plugin import (
    PluginInstallationResponse,
    PreparePluginInstallation,
)

_SCHEMA = "aap_inference_topology_v1"
_REPAIR_MODEL_LOCK_STATUSES = frozenset(
    {"ready", "backend_unavailable", "model_changed"}
)
_REGISTRY_FACTS = text(f"SELECT {_SCHEMA}.registry_facts(CAST(:registry_id AS uuid))")
_LOCK_PLUGIN_REGISTRY = text(
    f"SELECT {_SCHEMA}.lock_plugin_registry(CAST(:registry_id AS uuid))"
)
_PREPARE_PLUGIN = text(f"SELECT {_SCHEMA}.prepare_plugin_installation(CAST(:intent AS text))")
_GET_PLUGIN = text(f"SELECT {_SCHEMA}.plugin_installation(CAST(:id AS uuid))")
_LIST_PLUGINS = text(f"SELECT {_SCHEMA}.plugin_installations()")
_PLUGIN_EVIDENCE = text(f"SELECT {_SCHEMA}.plugin_installation_evidence(CAST(:id AS uuid))")
_COMMAND_PLUGIN = text(
    f"SELECT {_SCHEMA}.command_plugin_installation("
    "CAST(:id AS uuid),CAST(:revision AS integer),CAST(:action AS text),CAST(:evidence AS text))"
)
_REGISTRY_URL_OWNER = text(f"SELECT {_SCHEMA}.registry_url_owner(CAST(:url AS text))")
_REGISTRY_CATALOG = text(f"SELECT {_SCHEMA}.registry_catalog()")
_REGISTRY_NAMES = text(
    f"SELECT {_SCHEMA}.registry_names(CAST(:registry_ids AS uuid[]))"
)
_REGISTRY_REFERENCE = text(
    f"SELECT {_SCHEMA}.registry_reference(CAST(:registry_id AS uuid))"
)
_POOL_REFERENCE = text(f"SELECT {_SCHEMA}.pool_reference(CAST(:pool_id AS uuid))")
_POOL_FOR_REGISTRY = text(
    f"SELECT {_SCHEMA}.pool_for_registry(CAST(:registry_id AS uuid))"
)
_POOL_CATALOG = text(f"SELECT {_SCHEMA}.pool_catalog()")
_POOL_MEMBERS = text(f"SELECT {_SCHEMA}.pool_members(CAST(:pool_id AS uuid))")
_PROJECT_BACKEND_CATALOG = text(
    f"SELECT {_SCHEMA}.project_backend_catalog(CAST(:project_id AS uuid))"
)
_PROJECT_BACKEND_FACTS = text(
    f"SELECT {_SCHEMA}.project_backend_facts(CAST(:project_ids AS uuid[]))"
)
_ENABLED_PROJECT_IDS = text(
    f"SELECT {_SCHEMA}.enabled_project_ids(CAST(:project_ids AS uuid[]))"
)
_PROJECT_POOL_CATALOG = text(
    f"SELECT {_SCHEMA}.project_pool_catalog(CAST(:project_id AS uuid))"
)
_PROJECT_ASSOC_FOR_REGISTRY = text(
    f"SELECT {_SCHEMA}.project_assoc_for_registry(CAST(:project_id AS uuid),"
    "CAST(:registry_id AS uuid))"
)
_PROJECT_INTERACTIVE_BACKEND = text(
    f"SELECT {_SCHEMA}.project_interactive_backend(CAST(:project_id AS uuid))"
)
_CREATE_MANUAL_REGISTRY = text(
    f"SELECT {_SCHEMA}.create_manual_registry(CAST(:registry_id AS uuid),"
    "CAST(:values AS text))"
)
_SINGLETON_POOL = text(f"SELECT {_SCHEMA}.singleton_pool(CAST(:registry_id AS uuid))")
_CREATE_SINGLETON_POOL = text(
    f"SELECT {_SCHEMA}.create_singleton_pool(CAST(:registry_id AS uuid),"
    "CAST(:pool_id AS uuid),CAST(:name AS text),CAST(:snapshot AS text),"
    "CAST(:fingerprint AS text))"
)
_CREATE_SINGLETON_MEMBER = text(
    f"SELECT {_SCHEMA}.create_singleton_member(CAST(:registry_id AS uuid),"
    "CAST(:pool_id AS uuid),CAST(:member_id AS uuid))"
)
_PATCH_REGISTRY = text(
    f"SELECT {_SCHEMA}.patch_registry(CAST(:registry_id AS uuid),CAST(:patch AS text))"
)
_VALIDATE_REGISTRY_DELETE = text(
    f"SELECT {_SCHEMA}.validate_registry_delete(CAST(:registry_id AS uuid))"
)
_DELETE_REGISTRY = text(
    f"SELECT {_SCHEMA}.delete_registry(CAST(:registry_id AS uuid),"
    "CAST(:expected AS text))"
)
_CREATE_POOL = text(
    f"SELECT {_SCHEMA}.create_pool(CAST(:pool_id AS uuid),CAST(:name AS text))"
)
_PATCH_POOL = text(
    f"SELECT {_SCHEMA}.patch_pool(CAST(:pool_id AS uuid),CAST(:patch AS text))"
)
_LOCK_EMPTY_POOL = text(f"SELECT {_SCHEMA}.lock_empty_pool(CAST(:pool_id AS uuid))")
_DELETE_EMPTY_POOL = text(f"SELECT {_SCHEMA}.delete_empty_pool(CAST(:pool_id AS uuid))")
_LOCK_MEMBER_UPSERT = text(
    f"SELECT {_SCHEMA}.lock_member_upsert_rows(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid))"
)
_LOCK_REPAIR_MODEL_ROWS = text(
    f"SELECT {_SCHEMA}.lock_repair_model_rows(CAST(:project_id AS uuid),"
    "CAST(:pool_id AS uuid),CAST(:registry_ids AS uuid[]))"
)
_UPSERT_MEMBER = text(
    f"SELECT {_SCHEMA}.upsert_member(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid),CAST(:weight AS integer),CAST(:expected AS text))"
)
_REMOVE_MEMBER = text(
    f"SELECT {_SCHEMA}.remove_member(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid),CAST(:expected AS text))"
)
_DRAIN_MEMBER = text(
    f"SELECT {_SCHEMA}.drain_member(CAST(:pool_id AS uuid),CAST(:registry_id AS uuid))"
)
_RESUME_MEMBER = text(
    f"SELECT {_SCHEMA}.resume_member(CAST(:pool_id AS uuid),CAST(:registry_id AS uuid))"
)
_DRIFT_REVIEW_ROWS = text(
    f"SELECT {_SCHEMA}.drift_review_rows(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid))"
)
_LOCK_DRIFT_ROWS = text(
    f"SELECT {_SCHEMA}.lock_drift_acceptance_rows(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid))"
)
_ACCEPT_DRIFT = text(
    f"SELECT {_SCHEMA}.accept_drift(CAST(:pool_id AS uuid),"
    "CAST(:registry_id AS uuid),CAST(:candidate_fingerprint AS text),"
    "CAST(:enable_pool AS boolean),CAST(:expected AS text))"
)
_SET_PROJECT_REGISTRY = text(
    f"SELECT {_SCHEMA}.set_project_registry_enablement(CAST(:project_id AS uuid),"
    "CAST(:registry_id AS uuid),CAST(:enabled AS boolean),CAST(:overrides AS text))"
)
_SET_PROJECT_POOL = text(
    f"SELECT {_SCHEMA}.set_project_pool_enablement(CAST(:project_id AS uuid),"
    "CAST(:pool_id AS uuid),CAST(:enabled AS boolean),CAST(:overrides AS text))"
)
_DELETE_PROJECT_ASSOCIATIONS = text(
    f"SELECT {_SCHEMA}.delete_project_associations(CAST(:project_id AS uuid))"
)


def _json(value: Any) -> str:
    return json.dumps(
        to_record(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _object(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RuntimeError("Inference topology Native returned malformed facts")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeError("Inference topology Native returned malformed fact list")
    return value


def _driver_error(error: DBAPIError):
    """Yield the DBAPI error itself then the driver exception SQLAlchemy chained.

    The asyncpg dialect re-raises every server error as an adapted DBAPI error
    whose text is ``"<class 'asyncpg.exceptions.X'>: <server message>"`` and
    chains the original driver exception. Native messages must be read from the
    driver, never parsed out of the adapted wrapper text.
    """

    original = error.orig
    if original is None:
        return
    yield original
    cause = getattr(original, "__cause__", None)
    if cause is not None:
        yield cause


def _sqlstate(error: DBAPIError) -> str | None:
    for original in _driver_error(error):
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if state:
            return state
    return None


def _primary_message(error: DBAPIError) -> str:
    for original in _driver_error(error):
        diagnostic = getattr(original, "diag", None)
        primary = getattr(diagnostic, "message_primary", None)
        if isinstance(primary, str) and primary:
            return primary
        message = getattr(original, "message", None)
        if isinstance(message, str) and message:
            return message
    return str(getattr(error, "orig", "")) or "Inference topology operation failed"


class InferenceTopologyData:
    """Fixed topology RPCs bound to one caller-owned AsyncSession transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def flush(self) -> None:
        await self.db.flush()

    async def _scalar(self, statement, parameters=None, *, autoflush: bool = True):
        if autoflush and self.db.autoflush:
            await self.db.flush()
        try:
            return await self.db.scalar(
                statement.execution_options(autoflush=False), parameters or {}
            )
        except DBAPIError as error:
            self._raise_native_error(error)
            raise

    @staticmethod
    def _raise_native_error(error: DBAPIError) -> None:
        message = _primary_message(error)
        if message.startswith("aap_inference_topology:"):
            from aap_inference.topology import (
                CapabilityRevalidationError,
                ManagedMutationBlocked,
                PoolCapabilityUnavailable,
                PoolMemberConflict,
                RegistryDeleteBlocked,
                RegistryURLConflict,
                TopologyChanged,
            )

            fields = message.split(":", 3)
            code = fields[1] if len(fields) > 1 else "unknown"
            detail = fields[2] if len(fields) > 2 else "topology operation rejected"
            extra = fields[3] if len(fields) > 3 else ""
            if code == "pool_member_conflict":
                raise PoolMemberConflict(UUID(extra)) from error
            if code == "pool_capability_unavailable":
                raise PoolCapabilityUnavailable(detail) from error
            if code == "registry_url_conflict":
                raise RegistryURLConflict(detail) from error
            if code == "registry_delete_blocked":
                raise RegistryDeleteBlocked(int(extra or "0")) from error
            if code.startswith("capability_"):
                raise CapabilityRevalidationError(code, detail) from error
            if code == "topology_changed":
                raise TopologyChanged("ml_backend_topology_changed", detail) from error
            if code == "managed_gpu_mutation":
                raise ManagedMutationBlocked(detail) from error
            if code == "pool_has_members":
                raise ValueError(detail) from error
            if code == "project_pool_not_enabled":
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "ml_backend_pool_not_enabled",
                        "message": detail,
                    },
                ) from error
            if code == "project_topology_changed":
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "ml_backend_topology_changed",
                        "message": detail,
                    },
                ) from error
            if code == "registry_evaluation_reference":
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=409,
                    detail="推理实例被冻结样本评估引用，请先结束并删除评估",
                ) from error
        if message in {
            "service Pool not found",
            "pool not found",
            "registry not found",
        }:
            raise ValueError(message) from error
        if message == "disabled member needs capability re-validation before resume":
            raise ValueError(message) from error
        if "member upsert review became stale" in message:
            from aap_inference.topology import TopologyChanged

            raise TopologyChanged(
                "ml_backend_topology_changed",
                "service-pool topology changed while adding or updating a member",
            ) from error
        if _sqlstate(error) == "55P03":
            from fastapi import HTTPException

            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from error
        raise error

    async def _array_scalar(
        self, statement, parameters=None, *, autoflush: bool = True
    ):
        return _array(await self._scalar(statement, parameters, autoflush=autoflush))

    async def registry_facts(self, registry_id: UUID) -> RegistryPublicFacts | None:
        row = _object(await self._scalar(_REGISTRY_FACTS, {"registry_id": registry_id}))
        return RegistryPublicFacts.from_record(row) if row is not None else None

    async def lock_plugin_registry(self, registry_id: UUID) -> RegistryPublicFacts | None:
        row = _object(
            await self._scalar(_LOCK_PLUGIN_REGISTRY, {"registry_id": registry_id})
        )
        return RegistryPublicFacts.from_record(row) if row is not None else None

    async def _plugin_scalar(self, statement, parameters=None):
        try:
            return await self._scalar(statement, parameters)
        except DBAPIError as error:
            if _sqlstate(error) in {"22023", "22P02"}:
                raise ValueError(_primary_message(error)) from error
            raise

    async def prepare_plugin_installation(
        self, payload: PreparePluginInstallation
    ) -> PluginInstallationResponse:
        row = await self._plugin_scalar(
            _PREPARE_PLUGIN, {"intent": payload.model_dump_json()}
        )
        return PluginInstallationResponse.model_validate(_object(row))

    async def get_plugin_installation(
        self, installation_id: UUID
    ) -> PluginInstallationResponse | None:
        row = _object(await self._plugin_scalar(_GET_PLUGIN, {"id": installation_id}))
        return PluginInstallationResponse.model_validate(row) if row is not None else None

    async def list_plugin_installations(self) -> list[PluginInstallationResponse]:
        rows = _array(await self._plugin_scalar(_LIST_PLUGINS))
        return [PluginInstallationResponse.model_validate(row) for row in rows]

    async def get_plugin_installation_evidence(
        self, installation_id: UUID
    ) -> Mapping[str, Any] | None:
        return _object(await self._plugin_scalar(_PLUGIN_EVIDENCE, {"id": installation_id}))

    async def command_plugin_installation(
        self, installation_id: UUID, expected_revision: int, action: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> PluginInstallationResponse:
        validate_installation_command(expected_revision, action, evidence)
        if action == "retire":
            # Inference row first, then Learning's deployment locks: the same
            # order used by freeze and publish.  Keep both through commit.
            if await self.lock_plugin_registry(installation_id) is None:
                raise ValueError("plugin installation not found")
            from app.services import trained_models

            usage = await trained_models.registry_usage(self.db, installation_id)
            if usage.active_releases or usage.frozen_executions:
                raise ValueError("plugin installation has active Learning dependencies")
        row = await self._plugin_scalar(
            _COMMAND_PLUGIN,
            {"id": installation_id, "revision": expected_revision, "action": action,
             "evidence": _json(evidence) if evidence is not None else None},
        )
        return PluginInstallationResponse.model_validate(_object(row))

    async def registry_url_owner(self, url: str) -> RegistryName | None:
        row = _object(await self._scalar(_REGISTRY_URL_OWNER, {"url": url}))
        return RegistryName.from_record(row) if row is not None else None

    async def registry_catalog(self) -> list[RegistryPublicFacts]:
        return [
            RegistryPublicFacts.from_record(row)
            for row in await self._array_scalar(_REGISTRY_CATALOG)
        ]

    async def registry_names(self, registry_ids: Sequence[UUID]) -> dict[UUID, str]:
        identities = list(dict.fromkeys(registry_ids))
        if len(identities) > 500:
            raise ValueError("topology name batches are limited to 500 registry ids")
        if not identities:
            return {}
        rows = await self._array_scalar(_REGISTRY_NAMES, {"registry_ids": identities})
        return {
            fact.id: fact.name
            for fact in (RegistryName.from_record(row) for row in rows)
        }

    async def registry_reference(self, registry_id: UUID) -> RegistryReference | None:
        row = _object(
            await self._scalar(
                _REGISTRY_REFERENCE,
                {"registry_id": registry_id},
                autoflush=False,
            )
        )
        return RegistryReference.from_record(row) if row is not None else None

    async def pool_reference(self, pool_id: UUID) -> PoolFacts | None:
        row = _object(
            await self._scalar(_POOL_REFERENCE, {"pool_id": pool_id}, autoflush=False)
        )
        return PoolFacts.from_record(row) if row is not None else None

    async def pool_for_registry(self, registry_id: UUID) -> PoolFacts | None:
        row = _object(
            await self._scalar(_POOL_FOR_REGISTRY, {"registry_id": registry_id})
        )
        return PoolFacts.from_record(row) if row is not None else None

    async def pool_catalog(self) -> list[PoolFacts]:
        return [
            PoolFacts.from_record(row)
            for row in await self._array_scalar(_POOL_CATALOG)
        ]

    async def pool_members(self, pool_id: UUID) -> list[PoolMemberFacts]:
        rows = await self._array_scalar(_POOL_MEMBERS, {"pool_id": pool_id})
        return [PoolMemberFacts.from_record(row) for row in rows]

    async def project_backend_catalog(
        self, project_id: UUID
    ) -> list[ProjectBackendFacts]:
        rows = await self._array_scalar(
            _PROJECT_BACKEND_CATALOG, {"project_id": project_id}
        )
        return [ProjectBackendFacts.from_record(row) for row in rows]

    async def project_backend_facts(
        self, project_ids: Sequence[UUID]
    ) -> list[ProjectBackendFacts]:
        identities = list(dict.fromkeys(project_ids))
        if len(identities) > 500:
            raise ValueError("topology project batches are limited to 500 project ids")
        if not identities:
            return []
        rows = await self._array_scalar(
            _PROJECT_BACKEND_FACTS, {"project_ids": identities}
        )
        return [ProjectBackendFacts.from_record(row) for row in rows]

    async def enabled_project_ids(self, project_ids: Sequence[UUID]) -> list[UUID]:
        identities = list(dict.fromkeys(project_ids))
        if len(identities) > 500:
            raise ValueError("topology project batches are limited to 500 project ids")
        if not identities:
            return []
        rows = await self._array_scalar(
            _ENABLED_PROJECT_IDS, {"project_ids": identities}
        )
        return [UUID(str(project_id)) for project_id in rows]

    async def project_pool_catalog(self, project_id: UUID) -> list[ProjectPoolFacts]:
        rows = await self._array_scalar(
            _PROJECT_POOL_CATALOG, {"project_id": project_id}
        )
        return [ProjectPoolFacts.from_record(row) for row in rows]

    async def project_assoc_for_registry(
        self, project_id: UUID, registry_id: UUID
    ) -> ProjectAssociationFacts | None:
        row = _object(
            await self._scalar(
                _PROJECT_ASSOC_FOR_REGISTRY,
                {"project_id": project_id, "registry_id": registry_id},
            )
        )
        return ProjectAssociationFacts.from_record(row) if row is not None else None

    async def project_interactive_backend(
        self, project_id: UUID
    ) -> RegistryPublicFacts | None:
        row = _object(
            await self._scalar(_PROJECT_INTERACTIVE_BACKEND, {"project_id": project_id})
        )
        return RegistryPublicFacts.from_record(row) if row is not None else None

    async def create_manual_registry(
        self, registry_id: UUID, values: Mapping[str, Any]
    ) -> RegistryPublicFacts:
        row = _object(
            await self._scalar(
                _CREATE_MANUAL_REGISTRY,
                {"registry_id": registry_id, "values": _json(values)},
                autoflush=False,
            )
        )
        if row is None:
            raise RuntimeError("topology Registry create returned no public facts")
        return RegistryPublicFacts.from_record(row)

    async def singleton_pool(self, registry_id: UUID) -> PoolFacts | None:
        row = _object(await self._scalar(_SINGLETON_POOL, {"registry_id": registry_id}))
        return PoolFacts.from_record(row) if row is not None else None

    async def create_singleton_pool(
        self,
        registry_id: UUID,
        pool_id: UUID,
        name: str,
        snapshot: Mapping[str, Any] | None,
        fingerprint: str | None,
    ) -> PoolFacts:
        row = _object(
            await self._scalar(
                _CREATE_SINGLETON_POOL,
                {
                    "registry_id": registry_id,
                    "pool_id": pool_id,
                    "name": name,
                    "snapshot": _json(snapshot) if snapshot is not None else None,
                    "fingerprint": fingerprint,
                },
                autoflush=False,
            )
        )
        if row is None:
            raise RuntimeError("topology singleton creation returned no Pool")
        return PoolFacts.from_record(row)

    async def create_singleton_member(
        self, registry_id: UUID, pool_id: UUID, member_id: UUID
    ) -> PoolMemberFacts:
        row = _object(
            await self._scalar(
                _CREATE_SINGLETON_MEMBER,
                {
                    "registry_id": registry_id,
                    "pool_id": pool_id,
                    "member_id": member_id,
                },
                autoflush=False,
            )
        )
        if row is None:
            raise RuntimeError("topology singleton creation returned no member")
        return PoolMemberFacts.from_record(row)

    async def patch_registry(
        self, registry_id: UUID, patch: Mapping[str, Any]
    ) -> RegistryPublicFacts | None:
        row = _object(
            await self._scalar(
                _PATCH_REGISTRY,
                {"registry_id": registry_id, "patch": _json(patch)},
            )
        )
        await self.db.flush()
        return RegistryPublicFacts.from_record(row) if row is not None else None

    async def validate_registry_delete(self, registry_id: UUID) -> tuple[bool, int]:
        row = _object(
            await self._scalar(
                _VALIDATE_REGISTRY_DELETE,
                {"registry_id": registry_id},
            )
        )
        if row is None or set(row) != {"exists", "running_jobs"}:
            raise RuntimeError("topology delete validation returned malformed facts")
        if type(row["exists"]) is not bool or type(row["running_jobs"]) is not int:
            raise RuntimeError("topology delete validation returned malformed facts")
        return row["exists"], row["running_jobs"]

    async def delete_registry(
        self,
        registry_id: UUID,
        expected: RegistryTopologySnapshot | None,
    ) -> RegistryDeletion:
        row = _object(
            await self._scalar(
                _DELETE_REGISTRY,
                {
                    "registry_id": registry_id,
                    "expected": _json(expected.__dict__)
                    if expected is not None
                    else "null",
                },
            )
        )
        if row is None or set(row) != {"deleted", "pool_id"}:
            raise RuntimeError("topology delete returned malformed facts")
        if type(row["deleted"]) is not bool:
            raise RuntimeError("topology delete returned malformed facts")
        pool_id = UUID(row["pool_id"]) if row["pool_id"] is not None else None
        await self.db.flush()
        return RegistryDeletion(row["deleted"], pool_id)

    async def create_pool(self, pool_id: UUID, name: str) -> PoolFacts:
        row = _object(
            await self._scalar(
                _CREATE_POOL,
                {"pool_id": pool_id, "name": name},
            )
        )
        if row is None:
            raise RuntimeError("topology Pool create returned no facts")
        return PoolFacts.from_record(row)

    async def patch_pool(
        self, pool_id: UUID, patch: Mapping[str, Any]
    ) -> PoolFacts | None:
        row = _object(
            await self._scalar(
                _PATCH_POOL,
                {"pool_id": pool_id, "patch": _json(patch)},
            )
        )
        await self.db.flush()
        return PoolFacts.from_record(row) if row is not None else None

    async def delete_empty_pool(self, pool_id: UUID) -> bool:
        from app.services.workspace_backend_bindings import (
            clear_primary_backend_binding_for_pool,
        )

        locked = _object(await self._scalar(_LOCK_EMPTY_POOL, {"pool_id": pool_id}))
        if locked is None:
            return False
        await clear_primary_backend_binding_for_pool(self.db, pool_id)
        deleted = await self._scalar(
            _DELETE_EMPTY_POOL, {"pool_id": pool_id}, autoflush=False
        )
        await self.db.flush()
        if type(deleted) is not bool:
            raise RuntimeError("topology Pool delete returned malformed result")
        return deleted

    async def lock_member_upsert_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, RegistryPublicFacts | None, PoolMemberFacts | None]:
        row = _object(
            await self._scalar(
                _LOCK_MEMBER_UPSERT,
                {"pool_id": pool_id, "registry_id": registry_id},
            )
        )
        if row is None or set(row) != {"pool", "registry", "member"}:
            raise RuntimeError("topology member lock returned malformed facts")
        return (
            PoolFacts.from_record(row["pool"]) if row["pool"] is not None else None,
            RegistryPublicFacts.from_record(row["registry"])
            if row["registry"] is not None
            else None,
            PoolMemberFacts.from_record(row["member"])
            if row["member"] is not None
            else None,
        )

    async def lock_repair_model_rows(
        self,
        project_id: UUID,
        pool_id: UUID,
        registry_ids: Sequence[UUID],
    ) -> RepairModelLockStatus:
        """Share-lock a repair route until the caller-owned transaction ends.

        Pool and project/pool association have to be present and enabled for the
        route to be usable (``backend_unavailable`` otherwise). Requested and
        selected backends must each be an active member of that pool, and every
        registry must still exist (``model_changed`` otherwise). The Native
        function locks rows in a fixed order with ``FOR SHARE NOWAIT``; a lock
        conflict propagates as the driver error instead of becoming a status.
        """

        identities = list(dict.fromkeys(registry_ids))
        if not 1 <= len(identities) <= 2:
            raise ValueError(
                "repair model locks require one or two distinct registry ids"
            )
        if any(not isinstance(registry_id, UUID) for registry_id in identities):
            raise ValueError("repair model locks require UUID registry ids")
        if self.db.autoflush:
            await self.db.flush()
        status = await self.db.scalar(
            _LOCK_REPAIR_MODEL_ROWS.execution_options(autoflush=False),
            {
                "project_id": project_id,
                "pool_id": pool_id,
                "registry_ids": identities,
            },
        )
        if type(status) is not str or status not in _REPAIR_MODEL_LOCK_STATUSES:
            raise RuntimeError("topology repair model lock returned malformed status")
        return status

    async def upsert_member(
        self,
        pool_id: UUID,
        registry_id: UUID,
        weight: int,
        expected: Mapping[str, Any],
    ) -> tuple[PoolMemberFacts, PoolFacts]:
        row = _object(
            await self._scalar(
                _UPSERT_MEMBER,
                {
                    "pool_id": pool_id,
                    "registry_id": registry_id,
                    "weight": weight,
                    "expected": _json(expected),
                },
                autoflush=False,
            )
        )
        if row is None or set(row) != {"member", "pool"}:
            raise RuntimeError("topology member upsert returned malformed facts")
        await self.db.flush()
        return PoolMemberFacts.from_record(row["member"]), PoolFacts.from_record(
            row["pool"]
        )

    async def remove_member(
        self,
        pool_id: UUID,
        registry_id: UUID,
        expected: RegistryTopologySnapshot | None,
    ) -> bool:
        payload = None
        if expected is not None:
            payload = {
                "registry_id": str(expected.registry_id),
                "member_id": str(expected.member_id) if expected.member_id else None,
                "pool_id": str(expected.pool_id) if expected.pool_id else None,
                "routing_generation": expected.routing_generation,
                "traffic_state": expected.traffic_state,
            }
        result = await self._scalar(
            _REMOVE_MEMBER,
            {
                "pool_id": pool_id,
                "registry_id": registry_id,
                "expected": _json(payload),
            },
        )
        await self.db.flush()
        if type(result) is not bool:
            raise RuntimeError("topology member removal returned malformed result")
        return result

    async def drain_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None:
        row = _object(
            await self._scalar(
                _DRAIN_MEMBER,
                {"pool_id": pool_id, "registry_id": registry_id},
            )
        )
        await self.db.flush()
        return PoolMemberFacts.from_record(row) if row is not None else None

    async def resume_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None:
        row = _object(
            await self._scalar(
                _RESUME_MEMBER,
                {"pool_id": pool_id, "registry_id": registry_id},
            )
        )
        await self.db.flush()
        return PoolMemberFacts.from_record(row) if row is not None else None

    async def _drift_rows(
        self, statement, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, list[PoolMemberFacts]]:
        row = _object(
            await self._scalar(
                statement,
                {"pool_id": pool_id, "registry_id": registry_id},
                autoflush=True,
            )
        )
        if row is None or set(row) != {"pool", "members"}:
            raise RuntimeError("topology drift read returned malformed facts")
        pool = PoolFacts.from_record(row["pool"]) if row["pool"] is not None else None
        members = [PoolMemberFacts.from_record(item) for item in _array(row["members"])]
        return pool, members

    async def drift_review_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, list[PoolMemberFacts]]:
        return await self._drift_rows(_DRIFT_REVIEW_ROWS, pool_id, registry_id)

    async def lock_drift_acceptance_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, list[PoolMemberFacts]]:
        return await self._drift_rows(_LOCK_DRIFT_ROWS, pool_id, registry_id)

    async def accept_drift(
        self,
        pool_id: UUID,
        registry_id: UUID,
        expected_candidate_fingerprint: str,
        enable_pool: bool,
        expected: Mapping[str, Any],
    ) -> PoolFacts | None:
        row = _object(
            await self._scalar(
                _ACCEPT_DRIFT,
                {
                    "pool_id": pool_id,
                    "registry_id": registry_id,
                    "candidate_fingerprint": expected_candidate_fingerprint,
                    "enable_pool": enable_pool,
                    "expected": _json(expected),
                },
                autoflush=False,
            )
        )
        await self.db.flush()
        return PoolFacts.from_record(row) if row is not None else None

    async def lock_project_reference(self, project_id: UUID) -> None:
        from app.services.workspace_backend_bindings import (
            lock_backend_project_reference,
        )

        await lock_backend_project_reference(self.db, project_id)

    async def set_project_registry_enablement(
        self,
        project_id: UUID,
        registry_id: UUID,
        enabled: bool,
        overrides: Mapping[str, Any],
    ) -> ProjectAssociationFacts:
        row = _object(
            await self._scalar(
                _SET_PROJECT_REGISTRY,
                {
                    "project_id": project_id,
                    "registry_id": registry_id,
                    "enabled": enabled,
                    "overrides": _json(overrides),
                },
            )
        )
        await self.db.flush()
        if row is None:
            raise RuntimeError("topology registry enablement returned no association")
        return ProjectAssociationFacts.from_record(row)

    async def set_project_pool_enablement(
        self,
        project_id: UUID,
        pool_id: UUID,
        enabled: bool,
        overrides: Mapping[str, Any],
    ) -> ProjectAssociationFacts:
        row = _object(
            await self._scalar(
                _SET_PROJECT_POOL,
                {
                    "project_id": project_id,
                    "pool_id": pool_id,
                    "enabled": enabled,
                    "overrides": _json(overrides),
                },
            )
        )
        await self.db.flush()
        if row is None:
            raise RuntimeError("topology Pool enablement returned no association")
        return ProjectAssociationFacts.from_record(row)

    async def delete_project_associations(self, project_id: UUID) -> int:
        deleted = await self._scalar(
            _DELETE_PROJECT_ASSOCIATIONS, {"project_id": project_id}
        )
        if type(deleted) is not int:
            raise RuntimeError(
                "topology project association deletion returned malformed count"
            )
        return deleted

    async def seed_workbench_health(self, registry_id: UUID) -> None:
        await self._scalar(
            text(f"SELECT {_SCHEMA}.seed_workbench_health(CAST(:registry_id AS uuid))"),
            {"registry_id": registry_id},
        )
