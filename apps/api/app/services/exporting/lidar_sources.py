"""Canonical object identity, separate from task-specific export bindings."""


def canonical_identity(source):
    identity = {**source, "relation": None}
    if source.get("owner_kind") == "dataset_auxiliary":
        identity["item_id"] = None
        identity["item_facts"] = None
    return identity


def media_type(role):
    if role == "primary_lidar":
        return "point_cloud"
    if role in {"raw_lidar", "map"}:
        return role
    if isinstance(role, str) and role.startswith("camera_") and role[7:]:
        return "image"
    return None
