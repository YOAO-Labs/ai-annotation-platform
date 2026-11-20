"""Saved export targets shared by ZIP and independent-media owners."""

from app.services import managed_s3_target as shared


def managed_prefix(base):
    return shared.managed_prefix(base, purpose="export")


def valid_saved_root(root):
    historical = root.endswith(shared.HISTORICAL_EXPORT_NAMESPACE)
    namespace = (
        shared.HISTORICAL_EXPORT_NAMESPACE if historical else shared.EXPORT_NAMESPACE
    )
    return root.endswith(namespace) and root == shared.managed_prefix(
        root[: -len(namespace)], purpose="export", historical_export=historical
    )


async def target_for(db, location, *, require_enabled=True, saved_root=""):
    return await shared.target_for(
        db,
        location,
        purpose="export",
        require_enabled=require_enabled,
        historical_export=saved_root.endswith(shared.HISTORICAL_EXPORT_NAMESPACE),
    )


async def bound_bucket(db, location):
    # Inspect existing bindings, not new ZIP allocations. The longest legal
    # historical root still identifies its bucket without granting a new key.
    target = await shared.target_for(
        db, location, purpose="export", require_enabled=False, historical_export=True
    )
    return target.bucket


def probe_target(target):
    """Qualify the existing media cleanup's exact-prefix listing permissions."""
    result = shared.probe_target(target)
    import uuid

    prefix = target.key_prefix + ".probe-list-" + uuid.uuid4().hex + "/"
    with target.client() as client:
        visible = client.list_objects_v2(Bucket=target.bucket, Prefix=prefix, MaxKeys=1)
        versions = client.list_object_versions(
            Bucket=target.bucket, Prefix=prefix, MaxKeys=1
        )
        for response in (visible, versions):
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
                raise shared.error("export", "unavailable")
        if (
            visible.get("Contents")
            or versions.get("Versions")
            or versions.get("DeleteMarkers")
        ):
            raise shared.error("export", "probe_cleanup_unproven")
    return {**result, "list": True, "list_versions": True}
