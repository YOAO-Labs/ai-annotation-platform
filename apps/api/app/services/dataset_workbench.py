"""Stored observations for navigation; original reads always revalidate content."""

from fastapi import HTTPException
from sqlalchemy import and_, case, cast, func, literal, or_, select, String, union_all

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.original_cache import OriginalCacheEntry
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetRegistrationRun,
    DatasetSource,
    DatasetSourceChange,
    DatasetSourceItem,
    DatasetSourcePreview,
    DatasetSourceRevision,
    DatasetScanRun,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services.data_management.task_filters import (
    apply_task_visibility,
    compile_filter,
)
from app.services.dataset_source_access import source_scope_clause
from app.services.directory_policy import preview_policy_clause
from app.services.project_access import is_privileged_for_project
from app.services.project_aggregates import (
    managed_project_scope_clause,
    project_scope_clause,
)

STATES = ("verified", "missing", "changed", "preview_only", "unavailable", "unverified")


async def require_dataset(db, dataset_id):
    dataset = await db.get(Dataset, dataset_id, populate_existing=True)
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    return dataset


async def require_filter_scene(db, dataset_id, scene_id):
    row = (
        (
            await db.execute(
                select(Scene.id, Scene.display_id, Scene.name).where(
                    Scene.id == scene_id, Scene.dataset_id == dataset_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Scene 不存在或不属于当前数据集")
    return dict(row)


async def scene_options(db, dataset_id, *, limit, offset, search, selected_id):
    await require_dataset(db, dataset_id)
    selected = (
        await require_filter_scene(db, dataset_id, selected_id)
        if selected_id is not None
        else None
    )
    query = select(Scene.id, Scene.display_id, Scene.name).where(
        Scene.dataset_id == dataset_id
    )
    if search and search.strip():
        query = query.where(Scene.name.ilike(f"%{search.strip()}%"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(
        query.order_by(Scene.name, Scene.id).limit(limit).offset(offset)
    )
    return {
        "items": [dict(row) for row in rows.mappings()],
        "total": total,
        "selected": selected,
    }


def location_dataset_scope(location_id, actor):
    # EXISTS gives a unique business count even when one dataset has many sources.
    return (
        select(1)
        .select_from(DatasetSource)
        .where(
            DatasetSource.dataset_id == Dataset.id,
            DatasetSource.location_id == location_id,
            source_scope_clause(actor),
        )
        .exists()
    )


def visible_mapping_query(item_id, actor):
    ready = and_(
        DatasetSource.enabled.is_(True),
        DatasetSource.unregistered_at.is_(None),
        StorageLocation.enabled.is_(True),
        preview_policy_clause(),
        DatasetSourcePreview.state == "ready",
        DatasetSourcePreview.asset_id == DatasetSourceItem.asset_id,
        DatasetSourcePreview.source_sha256 == DatasetSourceItem.content_sha256,
    )
    return (
        select(DatasetSourceItem)
        .join(DatasetSource, DatasetSource.id == DatasetSourceItem.source_id)
        .join(StorageLocation, StorageLocation.id == DatasetSource.location_id)
        .outerjoin(
            DatasetSourcePreview,
            and_(
                DatasetSourcePreview.source_id == DatasetSourceItem.source_id,
                DatasetSourcePreview.path_digest == DatasetSourceItem.path_digest,
            ),
        )
        .where(DatasetSourceItem.dataset_item_id == item_id, source_scope_clause(actor))
        .order_by(case((ready, 0), else_=1), DatasetSourceItem.source_id)
    )


def observations(
    dataset_id,
    actor,
    *,
    preferred_source_id=None,
    reference_source_ids=None,
    item_ids=None,
    state_only=False,
):
    """One SQL relation for both filtering and display, without source I/O."""
    if dataset_id is None and reference_source_ids is None:
        raise ValueError("Observations require a dataset or explicit reference sources")
    if state_only:
        # Non-reference state depends on the managed original, not its optional
        # directory provenance. Reference state always uses its retained source.
        source_id = DatasetReference.source_id
    else:
        provenance_query = visible_mapping_query(
            DatasetItem.id, actor
        ).with_only_columns(DatasetSourceItem.source_id)
        if preferred_source_id is not None:
            provenance_query = provenance_query.where(
                DatasetSourceItem.source_id == preferred_source_id
            )
        provenance = provenance_query.limit(1).correlate(DatasetItem).scalar_subquery()
        source_id = func.coalesce(DatasetReference.source_id, provenance)
    accepted = (
        select(DatasetSourceRevision.accepted_at)
        .where(
            DatasetSourceRevision.source_id == DatasetReference.source_id,
            DatasetSourceRevision.item_id_at_accept == DatasetItem.id,
            DatasetSourceRevision.asset_id_at_accept == DatasetAsset.id,
        )
        .order_by(DatasetSourceRevision.acceptance_order.desc())
        .limit(1)
        .correlate(DatasetItem, DatasetAsset, DatasetReference)
    )
    accepted_at = func.coalesce(accepted.scalar_subquery(), DatasetItem.created_at)
    accepted_revision = accepted.with_only_columns(
        DatasetSourceRevision.id
    ).scalar_subquery()
    changes = (
        select(
            DatasetSourceChange.kind.label("kind"),
            DatasetScanRun.completed_at.label("observed_at"),
        )
        .join(
            DatasetRegistrationRun,
            DatasetRegistrationRun.id == DatasetSourceChange.run_id,
        )
        .join(DatasetScanRun, DatasetScanRun.id == DatasetRegistrationRun.scan_run_id)
        .where(
            DatasetSourceChange.before_item_id == DatasetItem.id,
            DatasetRegistrationRun.source_id == DatasetReference.source_id,
            DatasetRegistrationRun.comparison_complete.is_(True),
            # Registration can complete again after accepting a revalidation or
            # move. Its mutable completion time must not revive old evidence.
            DatasetSourceChange.before_revision_id == accepted_revision,
        )
        .order_by(
            DatasetScanRun.completed_at.desc().nulls_last(),
            DatasetScanRun.created_at.desc(),
            DatasetScanRun.id,
        )
        .limit(1)
        .correlate(DatasetItem, DatasetReference, DatasetAsset)
        .lateral("last_change")
    )
    replica = (
        select(DatasetAssetLocation.verified_at)
        .join(StorageLocation, StorageLocation.id == DatasetAssetLocation.location_id)
        .where(
            DatasetAssetLocation.asset_id == DatasetAsset.id,
            DatasetAssetLocation.role == "managed_original",
            DatasetAssetLocation.reference_state == "present",
            DatasetAssetLocation.object_key == DatasetItem.file_path,
            StorageLocation.enabled.is_(True),
        )
        .order_by(DatasetAssetLocation.verified_at.desc().nulls_last())
        .limit(1)
        .correlate(DatasetItem, DatasetAsset)
        .scalar_subquery()
    )
    manageable = source_scope_clause(actor)
    preview = and_(
        manageable,
        DatasetSource.enabled.is_(True),
        DatasetSource.unregistered_at.is_(None),
        StorageLocation.enabled.is_(True),
        preview_policy_clause(),
        (
            select(1)
            .select_from(DatasetSourcePreview)
            .join(
                DatasetSourceItem,
                and_(
                    DatasetSourceItem.source_id == DatasetSourcePreview.source_id,
                    DatasetSourceItem.path_digest == DatasetSourcePreview.path_digest,
                ),
            )
            .where(
                DatasetSourcePreview.source_id == DatasetSource.id,
                DatasetSourceItem.dataset_item_id == DatasetItem.id,
                DatasetSourcePreview.asset_id == DatasetAsset.id,
                DatasetSourcePreview.source_sha256 == DatasetAsset.checksum_value,
                DatasetSourcePreview.state == "ready",
            )
            .correlate(DatasetSource, DatasetItem, DatasetAsset)
            .exists()
        ),
    )
    ref = DatasetReference.dataset_item_id.is_not(None)
    verified = DatasetAsset.verification_state == "verified"
    disabled = or_(
        DatasetSource.enabled.is_(False),
        DatasetSource.unregistered_at.is_not(None),
        StorageLocation.enabled.is_(False),
    )
    replaced = (
        select(1)
        .select_from(DatasetSourceItem)
        .where(
            DatasetSourceItem.source_id == DatasetReference.source_id,
            DatasetSourceItem.path_digest == DatasetReference.path_digest,
            DatasetSourceItem.dataset_item_id != DatasetItem.id,
        )
        .correlate(DatasetItem, DatasetReference)
        .exists()
    )
    state = case(
        (and_(ref, replaced), "changed"),
        (and_(ref, disabled), case((preview, "preview_only"), else_="unavailable")),
        (and_(ref, changes.c.kind == "changed"), "changed"),
        (and_(ref, changes.c.kind == "not_seen"), "missing"),
        (
            and_(ref, changes.c.kind == "unreadable"),
            case((preview, "preview_only"), else_="unavailable"),
        ),
        (and_(ref, verified, DatasetSource.id.is_not(None)), "verified"),
        (and_(~ref, verified, replica.is_not(None)), "verified"),
        else_="unverified",
    )
    if state_only:

        def scoped(query):
            if dataset_id is not None:
                query = query.where(DatasetItem.dataset_id == dataset_id)
            if item_ids is not None:
                query = query.where(DatasetItem.id.in_(item_ids))
            return query

        reference_states = scoped(
            select(DatasetItem.id.label("item_id"), state.label("state"))
            .select_from(DatasetItem)
            .join(DatasetReference, DatasetReference.dataset_item_id == DatasetItem.id)
            .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
            .outerjoin(DatasetSource, DatasetSource.id == DatasetReference.source_id)
            .outerjoin(StorageLocation, StorageLocation.id == DatasetSource.location_id)
            .outerjoin(changes, literal(True))
        )
        if reference_source_ids is not None:
            return reference_states.where(
                DatasetReference.source_id.in_(reference_source_ids)
            ).subquery("item_observations")
        # Managed/unverified items have no reference availability history. Keep
        # source changes and preview checks on the reference branch only.
        managed_states = scoped(
            select(
                DatasetItem.id.label("item_id"),
                case(
                    (and_(verified, replica.is_not(None)), "verified"),
                    else_="unverified",
                ).label("state"),
            )
            .select_from(DatasetItem)
            .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
            .where(
                ~select(1)
                .select_from(DatasetReference)
                .where(DatasetReference.dataset_item_id == DatasetItem.id)
                .correlate(DatasetItem)
                .exists()
            )
        )
        return union_all(reference_states, managed_states).subquery("item_observations")
    from app.services.original_cache import storage_identity
    from app.services.storage import storage_service

    cache = (
        select(OriginalCacheEntry.verified_at, OriginalCacheEntry.expires_at)
        .join(
            StorageBinding,
            and_(
                StorageBinding.purpose == "media-cache",
                StorageBinding.location_id == OriginalCacheEntry.location_id,
                StorageBinding.bucket == OriginalCacheEntry.bucket,
                StorageBinding.generation == OriginalCacheEntry.binding_generation,
            ),
        )
        .join(
            DatasetAssetLocation,
            and_(
                DatasetAssetLocation.asset_id == OriginalCacheEntry.asset_id,
                DatasetAssetLocation.location_id == OriginalCacheEntry.location_id,
                DatasetAssetLocation.bucket == OriginalCacheEntry.bucket,
                DatasetAssetLocation.role == "original_cache",
                DatasetAssetLocation.reference_state == "present",
                DatasetAssetLocation.object_key
                == func.concat(
                    "original-cache/v1/",
                    OriginalCacheEntry.asset_id,
                    "/",
                    OriginalCacheEntry.sha256,
                    "/",
                    OriginalCacheEntry.id,
                ),
            ),
        )
        .where(
            ref,
            verified,
            manageable,
            ~disabled,
            DatasetSource.ownership_mode == "reference",
            DatasetSource.cache_policy == "on_demand",
            StorageLocation.config["cache_policy"].astext == "on_demand",
            StorageLocation.config["original_cache_quota_bytes"].astext != "0",
            OriginalCacheEntry.scope_location_id == DatasetSource.location_id,
            OriginalCacheEntry.source_id == DatasetReference.source_id,
            OriginalCacheEntry.asset_id == DatasetAsset.id,
            OriginalCacheEntry.sha256 == DatasetAsset.checksum_value,
            OriginalCacheEntry.size_bytes == DatasetAsset.size_bytes,
            OriginalCacheEntry.source_revision == DatasetSource.config_revision,
            OriginalCacheEntry.policy_revision == DatasetSource.policy_revision,
            OriginalCacheEntry.location_revision == StorageLocation.config_revision,
            OriginalCacheEntry.storage_identity == storage_identity(storage_service),
            OriginalCacheEntry.state == "ready",
            OriginalCacheEntry.expires_at > func.now(),
        )
        .correlate(
            DatasetItem, DatasetAsset, DatasetReference, DatasetSource, StorageLocation
        )
        .limit(1)
        .lateral("original_cache_record")
    )
    columns = [DatasetItem.id.label("item_id"), state.label("state")]
    if not state_only:
        columns.extend(
            [
                case(
                    (ref, func.coalesce(changes.c.observed_at, accepted_at)),
                    else_=replica,
                ).label("observed_at"),
                func.coalesce(preview, False).label("preview_available"),
                cache.c.verified_at.is_not(None).label("original_cache_recorded"),
                cache.c.verified_at.label("original_cache_recorded_at"),
                cache.c.expires_at.label("original_cache_expires_at"),
                case((manageable, DatasetSource.id), else_=None).label("source_id"),
                case((manageable, DatasetSource.name), else_=None).label("source_name"),
                case((manageable, DatasetSource.location_id), else_=None).label(
                    "location_id"
                ),
                ref.label("is_reference"),
                func.coalesce(manageable, False).label("can_manage"),
            ]
        )
    base = (
        select(*columns)
        .select_from(DatasetItem)
        .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .outerjoin(DatasetReference, DatasetReference.dataset_item_id == DatasetItem.id)
        .outerjoin(DatasetSource, DatasetSource.id == source_id)
        .outerjoin(StorageLocation, StorageLocation.id == DatasetSource.location_id)
        .outerjoin(changes, literal(True))
    )
    if not state_only:
        base = base.outerjoin(cache, literal(True))
    if dataset_id is not None:
        base = base.where(DatasetItem.dataset_id == dataset_id)
    if reference_source_ids is not None:
        base = base.where(DatasetReference.source_id.in_(reference_source_ids))
    if item_ids is not None:
        base = base.where(DatasetItem.id.in_(item_ids))
    return base.subquery("item_observations")


def availability(row):
    state = row["state"]
    actions = []
    if row["can_manage"] and row["source_id"]:
        actions.extend(["manage_source", "view_location"])
        if row["is_reference"] and (
            state == "verified" or row["original_cache_recorded"]
        ):
            actions.append("read_original")
    if row["preview_available"]:
        actions.append("read_preview")
    return {
        "state": state,
        "reason_code": {
            "verified": "verified_observation",
            "missing": "not_seen_in_complete_scan",
            "changed": "content_change_requires_review",
            "preview_only": "original_unavailable_preview_retained",
            "unavailable": "source_unavailable",
            "unverified": "not_verified",
        }[state],
        "observed_at": row["observed_at"],
        "preview_available": row["preview_available"],
        "original_cache_recorded": row["original_cache_recorded"],
        "original_cache_recorded_at": row["original_cache_recorded_at"],
        "original_cache_expires_at": row["original_cache_expires_at"],
        "source_id": row["source_id"],
        "source_name": row["source_name"],
        "location_id": row["location_id"],
        "allowed_actions": actions,
    }


async def require_filter_source(db, dataset_id, source_id, actor):
    visible = await db.scalar(
        select(DatasetSource.id).where(
            DatasetSource.id == source_id,
            DatasetSource.dataset_id == dataset_id,
            source_scope_clause(actor),
        )
    )
    if visible is None:
        raise HTTPException(404, "Dataset source not found")


async def item_query(
    db, dataset_id, actor, *, source_id=None, state=None, search=None, filters=None
):
    """Authorized item relation shared by pages and immutable training selection."""
    await require_dataset(db, dataset_id)
    obs = (
        observations(dataset_id, actor, preferred_source_id=source_id, state_only=True)
        if state
        else select(DatasetItem.id.label("item_id"))
        .where(DatasetItem.dataset_id == dataset_id)
        .subquery("item_membership")
    )
    query = select(obs)
    if source_id is not None:
        await require_filter_source(db, dataset_id, source_id, actor)
        membership = (
            select(1)
            .select_from(DatasetSourceItem)
            .where(
                DatasetSourceItem.dataset_item_id == obs.c.item_id,
                DatasetSourceItem.source_id == source_id,
            )
            .exists()
        )
        reference = (
            select(1)
            .select_from(DatasetReference)
            .where(
                DatasetReference.dataset_item_id == obs.c.item_id,
                DatasetReference.source_id == source_id,
            )
            .exists()
        )
        query = query.where(or_(membership, reference))
    if state:
        query = query.where(obs.c.state == state)
    if search:
        query = query.where(
            obs.c.item_id.in_(
                select(DatasetItem.id).where(DatasetItem.file_name.ilike(f"%{search}%"))
            )
        )
    if filters is not None:
        predicates = []
        if filters.scene_id is not None:
            await require_filter_scene(db, dataset_id, filters.scene_id)
            predicates.append(DatasetItem.scene_id == filters.scene_id)
        if filters.media_type:
            predicates.append(DatasetItem.file_type == filters.media_type)
        if filters.extension:
            # Logical filename, never the opaque reference key; only the final
            # path component can contribute an extension.
            extension = func.lower(func.substring(DatasetItem.file_name, r"[^/.]+$"))
            has_extension = DatasetItem.file_name.op("~")(r"[^/]+\.[^/.]+$")
            extensions = (
                ["jpg", "jpeg"] if filters.extension == "jpg" else [filters.extension]
            )
            predicates.extend([has_extension, extension.in_(extensions)])
        known = and_(DatasetItem.width > 0, DatasetItem.height > 0)
        if filters.dimension_state == "known":
            predicates.append(known)
        elif filters.dimension_state == "unknown":
            predicates.append(
                or_(
                    DatasetItem.width.is_(None),
                    DatasetItem.height.is_(None),
                    DatasetItem.width <= 0,
                    DatasetItem.height <= 0,
                )
            )
        has_range = False
        for axis in ("width", "height"):
            column = getattr(DatasetItem, axis)
            lower, upper = (
                getattr(filters, f"min_{axis}"),
                getattr(filters, f"max_{axis}"),
            )
            if lower is not None:
                predicates.append(column >= lower)
                has_range = True
            if upper is not None:
                predicates.append(column <= upper)
                has_range = True
        if has_range:
            predicates.append(known)
        if predicates:
            query = query.where(
                obs.c.item_id.in_(select(DatasetItem.id).where(*predicates))
            )
    return query, obs


async def project_filtered_item_ids(db, project_id, dataset_id, filters, actor):
    if not await db.scalar(
        select(ProjectDataset.project_id).where(
            ProjectDataset.project_id == project_id,
            ProjectDataset.dataset_id == dataset_id,
        )
    ):
        raise HTTPException(404, "数据集未关联当前项目")
    query, _ = await item_query(
        db,
        dataset_id,
        actor,
        source_id=filters.source_id,
        state=filters.availability,
        search=filters.search,
        filters=filters,
    )
    return select(query.subquery().c.item_id)


async def item_page(
    db,
    dataset_id,
    actor,
    *,
    limit,
    offset,
    source_id=None,
    state=None,
    search=None,
    item_id=None,
    filters=None,
):
    query, obs = await item_query(
        db,
        dataset_id,
        actor,
        source_id=source_id,
        state=state,
        search=search,
        filters=filters,
    )
    if item_id is not None:
        query = query.where(obs.c.item_id == item_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    members = (
        query.with_only_columns(obs.c.item_id)
        .join(DatasetItem, DatasetItem.id == obs.c.item_id)
        .order_by(DatasetItem.created_at.desc(), DatasetItem.id)
        .limit(limit)
        .offset(offset)
        .cte("dataset_page")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    details = observations(
        dataset_id,
        actor,
        preferred_source_id=source_id,
        item_ids=select(members.c.item_id),
    )
    rows = list(
        (
            await db.execute(
                select(details)
                .join(DatasetItem, DatasetItem.id == details.c.item_id)
                .order_by(DatasetItem.created_at.desc(), DatasetItem.id)
            )
        ).mappings()
    )
    from app.services.dataset import DatasetService

    # Reuse legacy media serialization only for the bounded page's identities.
    ids = [row["item_id"] for row in rows]
    items, _ = await DatasetService(db).list_items(
        dataset_id, limit=limit, item_ids=ids
    )
    by_id = {item["id"]: item for item in items}
    return [
        dict(by_id[row["item_id"]], availability=availability(row))
        for row in rows
        if row["item_id"] in by_id
    ], total


async def workbench(db, dataset_id, actor):
    await require_dataset(db, dataset_id)
    obs = observations(dataset_id, actor, state_only=True)
    counts = dict(
        (
            await db.execute(select(obs.c.state, func.count()).group_by(obs.c.state))
        ).all()
    )
    sources = list(
        await db.scalars(
            select(DatasetSource)
            .where(DatasetSource.dataset_id == dataset_id, source_scope_clause(actor))
            .order_by(DatasetSource.created_at, DatasetSource.id)
        )
    )
    scope = project_scope_clause(actor, Project.id)
    projects_query = (
        select(Project, ProjectMember.role)
        .join(ProjectDataset, ProjectDataset.project_id == Project.id)
        .outerjoin(
            ProjectMember,
            and_(
                ProjectMember.project_id == Project.id,
                ProjectMember.user_id == actor.id,
            ),
        )
        .where(ProjectDataset.dataset_id == dataset_id)
        .execution_options(populate_existing=True)
    )
    if scope is not None:
        projects_query = projects_query.where(scope)
    project_rows = (
        await db.execute(projects_query.order_by(Project.created_at, Project.id))
    ).all()
    projects = [project for project, _ in project_rows]
    groups = {}
    for project, role in project_rows:
        privileged = is_privileged_for_project(actor, project)
        groups.setdefault((privileged, None if privileged else role), []).append(
            project
        )
    dataset_filter = compile_filter(
        {"field": "dataset.dataset_id", "op": "eq", "value": str(dataset_id)},
        user=actor,
    )
    # Group equivalent access kinds so the canonical Data Manager predicates run
    # in one aggregate query, with at most manager + three membership branches.
    visible_queries = [
        apply_task_visibility(
            select(Task.id).where(
                Task.project_id.in_([project.id for project in group]),
                dataset_filter,
            ),
            actor,
            group[0],
            project_role=role,
        )
        for (_, role), group in groups.items()
    ]
    stats = {}
    if visible_queries:
        visible = union_all(*visible_queries).subquery("dataset_project_tasks")
        rows = (
            await db.execute(
                select(
                    Task.project_id,
                    func.count(Task.id),
                    func.count(Task.id).filter(Task.is_labeled.is_(True)),
                    func.count(Task.id).filter(Task.status == "completed"),
                    func.count(Task.id).filter(Task.status == "review"),
                )
                .join(visible, visible.c.id == Task.id)
                .group_by(Task.project_id)
            )
        ).all()
        stats = {row[0]: row[1:] for row in rows}
    return {
        "dataset_id": dataset_id,
        "availability_counts": {state: counts.get(state, 0) for state in STATES},
        "sources": [
            dict(
                id=s.id,
                name=s.name,
                location_id=s.location_id,
                enabled=s.enabled,
                unregistered_at=s.unregistered_at,
            )
            for s in sources
        ],
        "projects": [
            dict(
                id=p.id,
                name=p.name,
                display_id=p.display_id,
                type_label=p.type_label,
                **dict(
                    zip(
                        (
                            "total_tasks",
                            "labeled_tasks",
                            "completed_tasks",
                            "review_tasks",
                        ),
                        stats.get(p.id, (0, 0, 0, 0)),
                        strict=True,
                    )
                ),
                can_manage=is_privileged_for_project(actor, p),
            )
            for p in projects
        ],
    }


async def training_versions(db, dataset_id, actor, *, limit, offset):
    await require_dataset(db, dataset_id)
    version = TrainingDatasetVersion
    task_match = (
        select(1)
        .select_from(Task)
        .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
        .where(
            DatasetItem.dataset_id == dataset_id,
            Task.project_id == version.project_id,
            version.snapshot.contains(
                func.jsonb_build_object(
                    "samples",
                    func.jsonb_build_array(
                        func.jsonb_build_object("task_id", cast(Task.id, String))
                    ),
                )
            ),
        )
        .correlate(version)
        .exists()
    )
    match = or_(
        version.snapshot["source"]["dataset_id"].astext == str(dataset_id),
        version.snapshot.contains({"samples": [{"dataset_id": str(dataset_id)}]}),
        task_match,
    )
    query = (
        select(
            version.id,
            version.project_id,
            Project.name.label("project_name"),
            version.name,
            version.status,
            version.created_at,
        )
        .join(Project, Project.id == version.project_id)
        .where(match)
    )
    scope = managed_project_scope_clause(actor, version.project_id)
    if scope is not None:
        query = query.where(scope)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    items = (
        (
            await db.execute(
                query.order_by(version.created_at.desc(), version.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
