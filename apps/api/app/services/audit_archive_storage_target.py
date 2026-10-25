"""Purpose adapters for registered audit archive S3 targets."""

from app.services import managed_s3_target as shared
from app.services.managed_s3_target import object_params as object_params

MANAGED_NAMESPACE = "_aap_managed/audit-archive/"


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="audit-archive")


async def target_for(db, location, *, require_enabled=True):
    return await shared.target_for(
        db, location, purpose="audit-archive", require_enabled=require_enabled
    )


def version_id(response):
    return shared.version_id(response, purpose="audit-archive")


def _prove_absent(client, params):
    return shared._prove_absent(client, params, purpose="audit-archive")


def probe_target(target):
    return shared.probe_target(target)
