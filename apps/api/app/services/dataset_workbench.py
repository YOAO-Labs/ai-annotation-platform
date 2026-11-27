"""Stored observations for navigation; original reads always revalidate content."""

from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import (
    DateTime,
    Integer,
    Select,
    String,
    Uuid,
    and_,
    any_,
    cast,
    column,
    func,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import aliased

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.dataset_item_display import list_items
from app.services import agent_availability, assets_dataset_data
from app.services.data_management.task_filters import (
    apply_task_visibility,
    compile_filter,
)
from app.services.labeling_core_read_relations import task_row
from app.services.project_access import is_privileged_for_project
from app.services.project_aggregates import (
    managed_project_scope_clause,
    project_scope_clause,
)
from app.services.workspace_read_relations import (
    project_facts,
    project_member_facts,
    project_rows,
)

STATES = ("verified", "missing", "changed", "preview_only", "unavailable", "unverified")


async def require_dataset(db, dataset_id):
    dataset = await assets_dataset_data.dataset_metadata(db, dataset_id)
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    return dataset


async def require_filter_scene(db, dataset_id, scene_id):
    if db.autoflush:
        await db.flush()
    scenes = assets_dataset_data.dataset_scene_choices(dataset_id, scene_id)
    row = (
        (await db.execute(select(scenes).where(scenes.c.id == scene_id)))
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
    scenes = assets_dataset_data.dataset_scene_choices(dataset_id)
    query = select(scenes)
    if search and search.strip():
        query = query.where(scenes.c.name.ilike(f"%{search.strip()}%"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(
        query.order_by(scenes.c.name, scenes.c.id).limit(limit).offset(offset)
    )
    return {
        "items": [dict(row) for row in rows.mappings()],
        "total": total,
        "selected": selected,
    }


async def preferred_preview_mapping(db, dataset_id, item_id, actor):
    if db.autoflush:
        await db.flush()
    mapping = func.yoao_assets_v1.dataset_workbench_preview_mapping(
        cast(actor.id, Uuid), cast(dataset_id, Uuid), cast(item_id, Uuid)
    ).table_valued("source_id", "path_digest")
    return (await db.execute(select(mapping))).one_or_none()


def _uuid_array(ids):
    if isinstance(ids, Select):
        ids = func.array(ids.correlate(None).scalar_subquery())
    return cast(ids, ARRAY(Uuid))


def observations(
    dataset_id,
    actor,
    *,
    preferred_source_id=None,
    item_ids=None,
    state_only=False,
    cache_entry_ids=(),
):
    if state_only:
        states = func.yoao_assets_v1.dataset_workbench_item_states(
            cast(actor.id, Uuid), cast(dataset_id, Uuid)
        ).table_valued(column("item_id", Uuid), column("state", String))
        if item_ids is not None:
            return (
                select(states)
                .where(states.c.item_id == any_(_uuid_array(item_ids)))
                .subquery()
            )
        return states
    if item_ids is None:
        raise ValueError("Display observations require page members")
    return func.yoao_assets_v1.dataset_workbench_page_observations(
        cast(actor.id, Uuid),
        cast(dataset_id, Uuid),
        cast(preferred_source_id, Uuid),
        _uuid_array(item_ids),
        _uuid_array(cache_entry_ids),
    ).table_valued(
        column("item_id", Uuid),
        "state",
        "observed_at",
        "preview_available",
        "original_cache_entry_id",
        "original_cache_recorded",
        "original_cache_recorded_at",
        "original_cache_expires_at",
        "source_id",
        "source_name",
        "location_id",
        "is_reference",
        "can_manage",
        "agent_unavailable_reason",
        "reference_replica_recorded",
        "agent_state",
        "agent_reason_code",
        "agent_site_name",
        "agent_name",
        "agent_last_seen",
        "agent_measured_at",
    )


async def available_cache_ids(
    db, dataset_id, actor, *, reference_source_ids=None, item_ids=None
):
    """Check saved cache owners without opening provider clients."""
    from app.services import original_cache, storage_access
    from app.services.content_snapshot import ContentError

    if dataset_id is None and reference_source_ids is None:
        raise ValueError(
            "Cache evidence requires a dataset or explicit reference sources"
        )
    if db.autoflush:
        await db.flush()
    candidates = func.yoao_assets_v1.dataset_workbench_cache_candidates(
        cast(actor.id, Uuid),
        cast(dataset_id, Uuid),
        _uuid_array(reference_source_ids),
        _uuid_array(item_ids),
    ).table_valued(
        "id",
        "scope_location_id",
        "asset_id",
        "sha256",
        "size_bytes",
        "location_id",
        "bucket",
        "binding_generation",
        "storage_identity",
        column("storage_ref", JSONB),
    )
    records = await db.execute(select(candidates))
    sources, available = {}, []
    for data in records.mappings():
        record = SimpleNamespace(**data)
        try:
            await original_cache.prepare_read(db, record)
            if record.scope_location_id not in sources:
                sources[record.scope_location_id] = False
                await storage_access.check(db, record.scope_location_id)
                sources[record.scope_location_id] = True
            if sources[record.scope_location_id]:
                available.append(record.id)
        except (ContentError, ValueError):
            continue
    return available


def availability(row):
    state = row["state"]
    actions = []
    if row["can_manage"] and row["source_id"]:
        actions.extend(["manage_source", "view_location"])
        if row["is_reference"] and (
            state == "verified"
            or row["original_cache_recorded"]
            or row["reference_replica_recorded"]
        ):
            actions.append("read_original")
    if row["preview_available"]:
        actions.append("read_preview")
    return {
        "state": state,
        "reason_code": row["agent_unavailable_reason"]
        or {
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
        "agent_status": agent_availability.from_row(row),
        "allowed_actions": actions,
    }


async def require_filter_source(db, dataset_id, source_id, actor):
    if db.autoflush:
        await db.flush()
    sources = assets_dataset_data.visible_sources(
        actor.id, dataset_id=dataset_id, source_id=source_id
    )
    visible = await db.scalar(select(sources.c.id).where(sources.c.id == source_id))
    if visible is None:
        raise HTTPException(404, "Dataset source not found")


async def _item_matches(
    db, dataset_id, actor, *, source_id, state, search, filters, item_id=None
):
    await require_dataset(db, dataset_id)
    if source_id is not None:
        await require_filter_source(db, dataset_id, source_id, actor)
    scene_id = getattr(filters, "scene_id", None)
    if scene_id is not None:
        await require_filter_scene(db, dataset_id, scene_id)
    return func.yoao_assets_v1.dataset_workbench_item_matches(
        cast(actor.id, Uuid),
        cast(dataset_id, Uuid),
        cast(source_id, Uuid),
        cast(state or None, String),
        cast(search or None, String),
        cast(scene_id, Uuid),
        cast(getattr(filters, "media_type", None), String),
        cast(getattr(filters, "extension", None), String),
        cast(getattr(filters, "dimension_state", None), String),
        cast(getattr(filters, "min_width", None), Integer),
        cast(getattr(filters, "max_width", None), Integer),
        cast(getattr(filters, "min_height", None), Integer),
        cast(getattr(filters, "max_height", None), Integer),
        cast(item_id, Uuid),
    ).table_valued(
        column("item_id", Uuid), column("created_at", DateTime(timezone=True))
    )


async def project_filtered_item_ids(db, project_id, dataset_id, filters, actor):
    if not await assets_dataset_data.project_dataset_exists(db, project_id, dataset_id):
        raise HTTPException(404, "数据集未关联当前项目")
    matches = await _item_matches(
        db,
        dataset_id,
        actor,
        source_id=filters.source_id,
        state=filters.availability,
        search=filters.search,
        filters=filters,
    )
    return select(matches.c.item_id)


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
    matches = await _item_matches(
        db,
        dataset_id,
        actor,
        source_id=source_id,
        state=state,
        search=search,
        filters=filters,
        item_id=item_id,
    )
    total = await db.scalar(select(func.count()).select_from(matches))
    members = (
        select(matches.c.item_id, matches.c.created_at)
        .order_by(matches.c.created_at.desc(), matches.c.item_id)
        .limit(limit)
        .offset(offset)
        .cte("dataset_page")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    cache_ids = await available_cache_ids(
        db, dataset_id, actor, item_ids=select(members.c.item_id)
    )
    details = observations(
        dataset_id,
        actor,
        preferred_source_id=source_id,
        item_ids=select(members.c.item_id),
        cache_entry_ids=cache_ids,
    )
    rows = list(
        (
            await db.execute(
                select(details)
                .join(members, members.c.item_id == details.c.item_id)
                .order_by(members.c.created_at.desc(), members.c.item_id)
            )
        ).mappings()
    )
    ids = [row["item_id"] for row in rows]
    if not ids:
        return [], total
    items, _ = await list_items(db, dataset_id, limit=limit, item_ids=ids)
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
    visible_sources = assets_dataset_data.visible_sources(
        actor.id, dataset_id=dataset_id
    )
    sources = await db.execute(
        select(
            visible_sources.c.id,
            visible_sources.c.name,
            visible_sources.c.location_id,
            visible_sources.c.enabled,
            visible_sources.c.unregistered_at,
        ).order_by(visible_sources.c.created_at, visible_sources.c.id)
    )
    project_entity = aliased(Project, project_rows(), adapt_on_names=True)
    member_entity = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
    scope = project_scope_clause(actor, project_entity.id)
    links = assets_dataset_data.dataset_project_ids(dataset_id)
    projects_query = (
        select(project_entity, member_entity.role)
        .join(links, links.c.project_id == project_entity.id)
        .outerjoin(
            member_entity,
            and_(
                member_entity.project_id == project_entity.id,
                member_entity.user_id == actor.id,
            ),
        )
        .execution_options(populate_existing=True)
    )
    if scope is not None:
        projects_query = projects_query.where(scope)
    project_entries = (
        await db.execute(
            projects_query.order_by(project_entity.created_at, project_entity.id)
        )
    ).all()
    projects = [project for project, _ in project_entries]
    groups = {}
    for project, role in project_entries:
        privileged = is_privileged_for_project(actor, project)
        groups.setdefault((privileged, None if privileged else role), []).append(
            project
        )
    dataset_filter = compile_filter(
        {"field": "dataset.dataset_id", "op": "eq", "value": str(dataset_id)},
        user=actor,
    )
    visible_queries = [
        apply_task_visibility(
            select(task_row.id).where(
                task_row.project_id.in_([project.id for project in group]),
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
                    task_row.project_id,
                    func.count(task_row.id),
                    func.count(task_row.id).filter(task_row.is_labeled.is_(True)),
                    func.count(task_row.id).filter(task_row.status == "completed"),
                    func.count(task_row.id).filter(task_row.status == "review"),
                )
                .join(visible, visible.c.id == task_row.id)
                .group_by(task_row.project_id)
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
    from app.services.training_dataset_data import dataset_asset_versions

    await require_dataset(db, dataset_id)
    version = dataset_asset_versions(dataset_id, workbench=True).c
    project = project_facts()
    query = select(
        version.id,
        version.project_id,
        project.c.name.label("project_name"),
        version.name,
        version.status,
        version.created_at,
    ).join(project, project.c.id == version.project_id)
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
