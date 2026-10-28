"""Jobs owner facts about outstanding frozen deployment work."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text


@dataclass(frozen=True, slots=True)
class FrozenDeploymentUsage:
    active_jobs: int
    retryable_failures: int


async def frozen_deployment_usage(
    db, deployment_id: UUID, registry_id: UUID, model_version_id: UUID
) -> FrozenDeploymentUsage:
    row = (await db.execute(
        text("SELECT active_jobs, retryable_failures FROM "
             "aap_jobs_v1.frozen_deployment_usage(CAST(:deployment_id AS uuid), "
             "CAST(:registry_id AS uuid), CAST(:model_version_id AS uuid))"),
        {"deployment_id": deployment_id, "registry_id": registry_id,
         "model_version_id": model_version_id},
    )).one()
    return FrozenDeploymentUsage(int(row.active_jobs), int(row.retryable_failures))
