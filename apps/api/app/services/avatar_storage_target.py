"""Compatibility adapters for the reviewed avatar S3 target contract."""

from app.services import managed_s3_target as shared
from app.services import connector_guard as connector_guard
from app.services.managed_s3_target import (
    Target as Target,
    object_params as object_params,
)

MANAGED_NAMESPACE = "_aap_managed/avatars/"


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="avatars")


async def target_for(db, location, *, require_enabled=True):
    return await shared.target_for(
        db, location, purpose="avatars", require_enabled=require_enabled
    )


def version_id(response):
    return shared.version_id(response, purpose="avatars")


def _prove_absent(client, params):
    return shared._prove_absent(client, params, purpose="avatars")


def probe_target(target):
    return shared.probe_target(target)
