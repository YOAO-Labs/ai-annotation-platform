"""Read retained Learning dependencies while Inference holds the registry lock."""

import uuid

from yoao_jobs.domain_query_contract import FrozenDeploymentUsageRequest

from app.services import learning_models_data as data
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from app.services.trained_model_reference_contract import RegistryLearningUsage


async def registry_usage(db, registry_id: uuid.UUID) -> RegistryLearningUsage:
    """Lock Learning deployments and report live release and frozen-work facts.

    The caller must first hold Inference's registry lock in this transaction.
    """
    deployments = await data.lock_registry_deployments(db, registry_id)
    ids = [deployment.id for deployment in deployments]
    if not ids:
        return RegistryLearningUsage(active_releases=0, frozen_executions=0)
    active_releases = await data.count_active_releases(db, ids)
    frozen = 0
    for deployment in deployments:
        usage = await JobsDomainQueryPostgresData(db).frozen_deployment_usage(
            FrozenDeploymentUsageRequest(
                deployment_id=deployment.id,
                registry_id=deployment.registry_id,
                model_version_id=deployment.model_version_id,
            )
        )
        frozen += usage.active_jobs + usage.retryable_failures
    return RegistryLearningUsage(
        active_releases=int(active_releases), frozen_executions=frozen
    )
