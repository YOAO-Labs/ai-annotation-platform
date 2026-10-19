"""Keep native-owned state out of ORM migration autogeneration."""

NATIVE_MANAGED_TABLES = frozenset(
    {
        "asset_accepted_image_inputs",
        "asset_accepted_image_input_attempts",
        "asset_accepted_input_garbage",
        "asset_accepted_image_input_manifests",
        "asset_accepted_image_input_manifest_members",
        "annotation_input_sources",
        "asset_accepted_video_manifests",
        "asset_accepted_video_members",
        "asset_accepted_video_inputs",
        "asset_accepted_video_input_attempts",
        "asset_accepted_video_times",
        "asset_accepted_video_frames",
        "asset_accepted_video_frame_attempts",
        "annotation_video_input_sources",
        "video_tracker_input_scopes",
        "annotation_video_tracker_input_sources",
    }
)


def include_orm_schema_object(object_, name, type_, reflected, compare_to):
    """Native SQL migrations remain the sole schema owner for these relations."""
    return not (
        type_ == "table"
        and getattr(object_, "schema", None) in {None, "public"}
        and name in NATIVE_MANAGED_TABLES
    )
