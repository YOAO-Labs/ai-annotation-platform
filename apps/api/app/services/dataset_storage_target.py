"""Registered dataset original targets and their existing writer capabilities."""

from app.services import managed_s3_target as shared

MANAGED_NAMESPACE = "_aap_managed/datasets/"
MAX_MANAGED_PREFIX_CHARS = 891
MAX_MANAGED_PREFIX_BYTES = 915


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="datasets")


async def target_for(db, location, *, require_enabled=True):
    return await shared.target_for(
        db, location, purpose="datasets", require_enabled=require_enabled
    )
