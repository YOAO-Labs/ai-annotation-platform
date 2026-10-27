"""Import target qualification for existing version-owned temporary inputs."""

import uuid

from app.services import managed_s3_target as shared

MANAGED_NAMESPACE = "_aap_managed/import/"
MAX_MANAGED_PREFIX_BYTES = 645


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="import")


async def target_for(db, location, *, require_enabled=True):
    return await shared.target_for(
        db, location, purpose="import", require_enabled=require_enabled
    )


def probe_target(target):
    with target.client() as client:
        result = client.get_bucket_versioning(Bucket=target.bucket)
        if result.get("Status") != "Enabled":
            raise shared.error("import", "versioning_required")
    result = shared.probe_target(target)
    prefix = target.key_prefix + ".probe-list-" + uuid.uuid4().hex + "/"
    with target.client() as client:
        for response in (
            client.list_objects_v2(Bucket=target.bucket, Prefix=prefix, MaxKeys=1),
            client.list_object_versions(Bucket=target.bucket, Prefix=prefix, MaxKeys=1),
        ):
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
                raise shared.error("import", "unavailable")
            if (
                response.get("Contents")
                or response.get("Versions")
                or response.get("DeleteMarkers")
            ):
                raise shared.error("import", "probe_cleanup_unproven")
    return {**result, "versioned": True, "list": True, "list_versions": True}
