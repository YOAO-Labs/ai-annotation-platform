"""Current actor policy and durable delegated authority for mask repair."""

from app.services.content_snapshot import ContentError


async def authorize_repair_task(db, task, actor, *, ai=False):
    """Read-only repair policy, shared before I/O and under final owner locks."""
    from app.services import task_access
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.labeling_core_read_relations import get_core_row, batch_row
    from app.services.project_access import (
        get_workspace_project,
        resolve_project_access,
    )
    from app.services.project_write_guard import assert_phase_write_allowed
    from app.services.raster_mask_storage import assert_raster_mask_write_enabled
    from app.services.task_lock import TaskLockService

    actor = await IdentityAccountLockData(db).read_actor_status(actor.id)
    project = await get_workspace_project(
        db, actor_id=actor.id, project_id=task.project_id, populate_existing=True
    )
    if actor is None or not actor.is_active or project is None:
        raise ContentError("repair_actor_unavailable", 403)
    if task.batch_id:
        await get_core_row(db, batch_row, task.batch_id, populate_existing=True)
    access = await resolve_project_access(db, actor_id=actor.id, project=project)
    if not access.has("review.write"):
        raise ContentError("repair_authority_revoked", 403)
    await task_access.assert_task_visible(db, task, actor.id, access=access)
    await assert_phase_write_allowed(
        db, task, actor, action="repair mask", lock_membership=False
    )
    if any(
        lock.user_id != actor.id
        for lock in await TaskLockService(db).active_locks(task.id, cleanup=False)
    ):
        raise ContentError("task_lock_conflict")
    if task.file_type == "image":
        assert_raster_mask_write_enabled(project)
    if ai and project.ai_interactive_enabled is False:
        raise ContentError("ai_interactive_disabled", 403)
    return actor


def unit_items(batch, section, unit):
    if section == "candidates":
        return [unit]
    return [
        (batch.plan_json or {})["items"][int(index)]
        for index in unit.get("item_indexes") or []
    ]


def pending_reference_items(batch, *, rollback=False):
    plan, result = batch.plan_json or {}, batch.result_json or {}
    items = plan.get("items") or []
    selected = []
    for shard in plan.get("shards") or []:
        completed = ((result.get("shards") or {}).get(str(shard["id"])) or {}).get(
            "status"
        ) == "completed"
        undone = (
            ((result.get("rollback") or {}).get("shards") or {}).get(str(shard["id"]))
            or {}
        ).get("status") != "completed"
        if (rollback and completed and undone) or (not rollback and not completed):
            selected.extend(
                items[int(index)] for index in shard.get("item_indexes") or []
            )
    if not rollback:
        selected.extend(
            item
            for index, item in enumerate(items)
            if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
            and ((result.get("candidates") or {}).get(str(index)) or {}).get("status")
            != "completed"
        )
    return selected


def unwrap_authority(value, source):
    """An explicit mixed envelope never upgrades an old managed delegation."""
    if not isinstance(value, dict):
        return None
    if type(value.get("schema")) is int and value["schema"] == 2:
        if set(value) != {"schema", "reference", "managed"}:
            raise ContentError("repair_authority_revoked", 403)
        return value[source]
    if (
        source == "reference"
        and type(value.get("schema")) is int
        and value["schema"] == 1
    ):
        return value
    return None
