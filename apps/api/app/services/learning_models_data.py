"""Learning model facts and commands on the caller's PostgreSQL transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import bindparam, column, table, text
from sqlalchemy.dialects.postgresql import JSONB

_SCHEMA = "yoao_learning_v1"


@dataclass(frozen=True, slots=True)
class ModelVersion:
    id: UUID
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    request_id: UUID
    request_digest: str
    name: str
    framework: str
    framework_version: str
    artifact: dict
    classes: list[str]
    bindings: list[str]
    binding_digest: str
    provenance: dict
    evaluation: dict
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Deployment:
    id: UUID
    model_version_id: UUID
    registry_id: UUID
    check_id: UUID
    status: str
    receipt: dict
    trial: dict
    error_message: str | None
    checked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Release:
    id: UUID
    project_id: UUID
    model_version_id: UUID | None
    deployment_id: UUID | None
    creator_id: UUID | None
    previous_release_id: UUID | None
    rollback_from_id: UUID | None
    request_id: UUID
    request_digest: str
    generation: int
    task: str
    snapshot: dict
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    id: UUID
    project_id: UUID
    model_version_id: UUID
    creator_id: UUID | None
    creator_label: str
    sequence: int
    request_id: UUID
    request_digest: str
    disposition: str
    note: str
    created_at: datetime


model_lineage_row = table(
    "model_version_lineage",
    *map(
        column,
        (
            "id",
            "project_id",
            "run_id",
            "attempt_id",
            "name",
            "created_at",
            "binding_digest",
            "framework",
            "weights_sha256",
            "frozen_run_id",
            "frozen_attempt_id",
            "frozen_dataset_id",
            "frozen_dataset_digest",
            "frozen_validation_digest",
            "frozen_config_digest",
        ),
    ),
    schema=_SCHEMA,
)
deployment_lineage_row = table(
    "model_deployment_lineage",
    *map(
        column,
        (
            "id",
            "project_id",
            "model_version_id",
            "registry_id",
            "status",
            "checked_at",
            "created_at",
        ),
    ),
    schema=_SCHEMA,
)
release_lineage_row = table(
    "model_release_lineage",
    *map(
        column,
        (
            "id",
            "project_id",
            "model_version_id",
            "deployment_id",
            "generation",
            "previous_release_id",
            "rollback_from_id",
            "created_at",
            "frozen_model_version_id",
            "frozen_deployment_id",
            "frozen_sha256",
            "frozen_reference_digest",
        ),
    ),
    schema=_SCHEMA,
)


def _json_call(sql: str, *fields: str):
    return text(sql).bindparams(
        *(bindparam(name, type_=JSONB(none_as_null=True)) for name in fields)
    )


async def _one(db, statement, dto=None, **params):
    row = (await db.execute(statement, params)).mappings().one_or_none()
    return dto(**row) if row is not None and dto is not None else row


async def _all(db, statement, dto=None, **params):
    result = await db.execute(statement, params)
    return (
        [dto(**row) for row in result.mappings()] if dto is not None else list(result)
    )


async def get_version(
    db, project_id: UUID, version_id: UUID, *, key_share: bool = False
) -> ModelVersion | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.model_version_detail(:project, :version, :hold)"
        ),
        ModelVersion,
        project=project_id,
        version=version_id,
        hold=key_share,
    )


async def registration_replay(
    db,
    project_id: UUID,
    *,
    request_id: UUID | None = None,
    attempt_id: UUID | None = None,
) -> ModelVersion | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.model_registration_replay(:project, :request, :attempt)"
        ),
        ModelVersion,
        project=project_id,
        request=request_id,
        attempt=attempt_id,
    )


_REGISTER = _json_call(
    "SELECT * FROM yoao_learning_v1.register_model_candidate("
    ":project_id, :run_id, :attempt_id, :creator_id, :request_id, :request_digest, :name, "
    ":framework, :framework_version, :artifact, :classes, :bindings, :binding_digest, :provenance, :evaluation)",
    "artifact",
    "classes",
    "bindings",
    "provenance",
    "evaluation",
)


async def register_candidate(
    db,
    *,
    project_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    creator_id: UUID,
    request_id: UUID,
    request_digest: str,
    name: str,
    framework: str,
    framework_version: str,
    artifact: dict,
    classes: list[str],
    bindings: list[str],
    binding_digest: str,
    provenance: dict,
    evaluation: dict,
) -> ModelVersion | None:
    return await _one(
        db,
        _REGISTER,
        ModelVersion,
        project_id=project_id,
        run_id=run_id,
        attempt_id=attempt_id,
        creator_id=creator_id,
        request_id=request_id,
        request_digest=request_digest,
        name=name,
        framework=framework,
        framework_version=framework_version,
        artifact=artifact,
        classes=classes,
        bindings=bindings,
        binding_digest=binding_digest,
        provenance=provenance,
        evaluation=evaluation,
    )


async def run_candidates(db, project_id: UUID, run_id: UUID):
    return await _all(
        db,
        text("SELECT * FROM yoao_learning_v1.run_model_candidates(:project, :run)"),
        project=project_id,
        run=run_id,
    )


async def run_has_candidate(db, run_id: UUID, attempt_id: UUID | None = None) -> bool:
    return await db.scalar(
        text("SELECT yoao_learning_v1.run_has_model_candidate(:run, :attempt)"),
        {"run": run_id, "attempt": attempt_id},
    )


async def catalog_models(
    db, project_ids: list[UUID], *, include_candidates: bool
) -> list[ModelVersion]:
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.catalog_model_candidates(:projects, :include)"
        ),
        ModelVersion,
        projects=project_ids,
        include=include_candidates,
    )


async def candidate_visibility(db, project_id: UUID, version_id: UUID):
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_learning_v1.model_candidate_visibility(:project, :version)"
            ),
            {"project": project_id, "version": version_id},
        )
    ).one_or_none()


async def candidate_metadata(db, project_ids: list[UUID], version_ids: list[UUID]):
    if not version_ids:
        return []
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.model_candidate_metadata(:projects, :versions)"
        ),
        projects=project_ids,
        versions=version_ids,
    )


async def list_models(
    db,
    project_id: UUID,
    *,
    archive,
    pattern,
    framework,
    task,
    start,
    end,
    limit,
    offset,
):
    params = dict(
        project=project_id,
        archive=archive,
        pattern=pattern,
        framework=framework,
        task=task,
        start=start,
        end=end,
    )
    total = await db.scalar(
        text(
            "SELECT yoao_learning_v1.count_model_candidates(:project, :archive, :pattern, :framework, :task, :start, :end)"
        ),
        params,
    )
    items = await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.list_model_candidates("
            ":project, :archive, :pattern, :framework, :task, :start, :end, :limit, :offset)"
        ),
        ModelVersion,
        **params,
        limit=limit,
        offset=offset,
    )
    return total, items


async def deployments_for_versions(
    db, project_ids: list[UUID], version_ids: list[UUID]
) -> list[Deployment]:
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.model_deployments_for_versions(:projects, :versions)"
        ),
        Deployment,
        projects=project_ids,
        versions=version_ids,
    )


async def get_deployment(
    db, project_id: UUID, version_id: UUID, deployment_id: UUID, *, lock: bool = False
) -> Deployment | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.model_deployment_detail(:project, :version, :deployment, :lock)"
        ),
        Deployment,
        project=project_id,
        version=version_id,
        deployment=deployment_id,
        lock=lock,
    )


async def lock_deployment_for_registry(
    db, project_id: UUID, version_id: UUID, registry_id: UUID, *, ready: bool = False
) -> Deployment | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.hold_model_deployment_for_registry(:project, :version, :registry, :ready)"
        ),
        Deployment,
        project=project_id,
        version=version_id,
        registry=registry_id,
        ready=ready,
    )


async def lock_registry_deployments(db, registry_id: UUID):
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.hold_registry_model_deployments(:registry)"
        ),
        registry=registry_id,
    )


async def count_active_releases(db, deployment_ids: list[UUID]) -> int:
    return await db.scalar(
        text("SELECT yoao_learning_v1.count_active_model_releases(:deployments)"),
        {"deployments": deployment_ids},
    )


async def lock_candidate(
    db, project_id: UUID, model_version_id: UUID, deployment_id: UUID
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.hold_model_candidate(:project, :version, :deployment)"
        ),
        dict(project=project_id, version=model_version_id, deployment=deployment_id),
    )


async def lock_prediction_reference(
    db,
    project_id: UUID,
    model_version_id: UUID,
    deployment_id: UUID,
    release_id: UUID | None,
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.hold_prediction_model_reference(:project, :version, :deployment, :release)"
        ),
        dict(
            project=project_id,
            version=model_version_id,
            deployment=deployment_id,
            release=release_id,
        ),
    )


async def start_deployment_check(
    db, project_id: UUID, version_id: UUID, registry_id: UUID, check_id: UUID
) -> Deployment | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.start_model_deployment_check(:project, :version, :registry, :check)"
        ),
        Deployment,
        project=project_id,
        version=version_id,
        registry=registry_id,
        check=check_id,
    )


_FINISH_CHECK = _json_call(
    "SELECT * FROM yoao_learning_v1.finish_model_deployment_check("
    ":project, :deployment, :check, :receipt, :trial, :error, :at)",
    "receipt",
    "trial",
)


async def finish_deployment_check(
    db,
    project_id: UUID,
    deployment_id: UUID,
    check_id: UUID,
    *,
    receipt: dict,
    trial: dict,
    error: str | None,
    at: datetime,
) -> Deployment | None:
    return await _one(
        db,
        _FINISH_CHECK,
        Deployment,
        project=project_id,
        deployment=deployment_id,
        check=check_id,
        receipt=receipt,
        trial=trial,
        error=error,
        at=at,
    )


async def advance_deployment_retirement(
    db, project_id: UUID, deployment: Deployment, *, terminal: bool
):
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_learning_v1.advance_model_deployment_retirement("
                ":project, :version, :deployment, :check, :terminal)"
            ),
            dict(
                project=project_id,
                version=deployment.model_version_id,
                deployment=deployment.id,
                check=deployment.check_id,
                terminal=terminal,
            ),
        )
    ).one_or_none()


async def get_release(db, project_id: UUID, release_id: UUID) -> Release | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.model_release_detail(:project, :release)"),
        Release,
        project=project_id,
        release=release_id,
    )


async def release_replay(db, project_id: UUID, request_id: UUID) -> Release | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.model_release_replay(:project, :request)"),
        Release,
        project=project_id,
        request=request_id,
    )


async def catalog_releases(db, project_ids: list[UUID], version_ids: list[UUID]):
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.catalog_model_releases(:projects, :versions)"
        ),
        projects=project_ids,
        versions=version_ids,
    )


async def get_selection(db, project_id: UUID, task: str, *, lock: bool = False):
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_learning_v1.project_model_selection(:project, :task, :hold)"
            ),
            dict(project=project_id, task=task, hold=lock),
        )
    ).one_or_none()


async def list_releases(db, project_id: UUID, task: str, limit: int, offset: int):
    params = dict(project=project_id, task=task)
    total = await db.scalar(
        text("SELECT yoao_learning_v1.count_project_model_releases(:project, :task)"),
        params,
    )
    items = await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.list_project_model_releases(:project, :task, :limit, :offset)"
        ),
        Release,
        **params,
        limit=limit,
        offset=offset,
    )
    return total, items


_PUBLISH = _json_call(
    "SELECT * FROM yoao_learning_v1.publish_project_model_release("
    ":project, :task, :creator, :request, :digest, :release, :generation, "
    ":previous, :rollback, :version, :deployment, :snapshot)",
    "snapshot",
)


async def publish_release(
    db,
    *,
    project_id: UUID,
    actor_id: UUID,
    request_id: UUID,
    request_digest: str,
    release_id: UUID,
    task: str,
    generation: int,
    previous: UUID | None,
    rollback: UUID | None,
    version_id: UUID | None,
    deployment_id: UUID | None,
    snapshot: dict,
) -> Release | None:
    return await _one(
        db,
        _PUBLISH,
        Release,
        project=project_id,
        task=task,
        creator=actor_id,
        request=request_id,
        digest=request_digest,
        release=release_id,
        generation=generation,
        previous=previous,
        rollback=rollback,
        version=version_id,
        deployment=deployment_id,
        snapshot=snapshot,
    )


async def append_decision(
    db,
    project_id: UUID,
    model_id: UUID,
    *,
    actor_id: UUID,
    actor_label: str,
    request_id: UUID,
    request_digest: str,
    disposition: str,
    note: str,
) -> Decision | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.append_model_decision("
            ":project, :model, :actor, :label, :request, :digest, :disposition, :note)"
        ),
        Decision,
        project=project_id,
        model=model_id,
        actor=actor_id,
        label=actor_label,
        request=request_id,
        digest=request_digest,
        disposition=disposition,
        note=note,
    )


async def list_decisions(
    db, project_id: UUID, model_id: UUID, before: int | None, limit: int
) -> list[Decision]:
    return await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.list_model_decisions(:project, :model, :before, :limit)"
        ),
        Decision,
        project=project_id,
        model=model_id,
        before=before,
        limit=limit,
    )
