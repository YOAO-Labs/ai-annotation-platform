"""Selection review cannot alter locators or silently reuse old work."""

import copy
from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetSourceItem
from app.schemas.dataset_source import (
    SourceRulesPreview as Preview,
    SourceRulesApply as Apply,
)
from app.services import (
    dataset_source_rules as rules,
    dataset_source_changes as changes,
    dataset_source_revisions as revisions,
    dataset_source_sync as sync,
    dataset_registration as registration,
)
from app.services.filesystem_storage import FilesystemStorageError
from app.services.source_selection import selected_file
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_source_changes import (
    rescan,
    preflight,
    accept_all_files,
    picture,
)
from tests.test_dataset_source_relocations import moved_candidate
from app.services.dataset_references import register_entry
from app.schemas.dataset_source import SourceSyncScheduleUpdate


def payload(world, **kwargs):
    return Preview(
        **{
            **dict(
                expected_revision=world[5].config_revision,
                recursive=True,
                include_globs=["*"],
                exclude_globs=[],
            ),
            **kwargs,
        }
    )


async def apply(db, world, value, impact):
    return await rules.apply(
        db,
        world[5].id,
        Apply(**value.model_dump(), impact_proof=impact["proof"]),
        world[2],
    )


async def test_rules_preserve_task_content_and_invalidate_preflight_and_plan(
    db_session, reference_world, httpx_client, annotator
):
    db = db_session
    old, _, task, world = reference_world
    run = await preflight(db, world)
    run_id = run.id
    row = await sync.configure(
        db,
        world[5].id,
        SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
        world[2],
    )
    await db.commit()
    row.scan_id, row.registration_id, row.state = world[6].id, run.id, "awaiting_review"
    original_config = copy.deepcopy(world[5].config)
    value = payload(world, exclude_globs=["*.png"])
    impact = await rules.preview(db, world[5].id, value, world[2])
    assert impact["counts"]["leaving_scope"] == 1
    await apply(db, world, value, impact)
    await db.commit()
    assert world[5].config["relative_path"] == original_config["relative_path"]
    assert (
        not row.enabled
        and row.state == "blocked"
        and row.last_error == "configuration_changed"
    )
    assert row.scan_id is None and row.registration_id is None
    await sync.configure(
        db,
        world[5].id,
        SourceSyncScheduleUpdate(expected_revision=row.revision, enabled=True),
        world[2],
    )
    assert row.state == "idle"
    r = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content",
        params={"sha256": old["sha256"]},
        headers=headers(annotator),
    )
    assert r.status_code == 200 and r.content == world[1]
    with pytest.raises(FilesystemStorageError, match="configuration_changed"):
        await registration.confirm(db, run_id, world[2], scope="additions_only")
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_parent_directory_rule_marks_omitted_accepted_descendants_excluded(
    db_session, content_world
):
    db, world = db_session, content_world
    (world[0] / "hidden").mkdir()
    (world[0] / "hidden/child.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    await accept_all_files(db, world)
    value = payload(world, exclude_globs=["hidden"])
    impact = await rules.preview(db, world[5].id, value, world[2])
    assert (
        impact["counts"]["leaving_scope"] == 1 and impact["counts"]["within_scope"] == 1
    )
    await apply(db, world, value, impact)
    await db.commit()
    world = await rescan(db, world)
    run = await preflight(db, world)
    page = await changes.page(db, run, None, 20, 0)
    child = next(c for c in page["items"] if c["relative_path"] == "hidden/child.png")
    assert child["kind"] == "excluded" and child["reason"] == "outside_selection_rules"
    assert page["counts"]["not_seen"] == 0
    assert await db.scalar(select(func.count()).select_from(DatasetSourceItem)) == 2


async def test_head_move_invalidates_impact_even_when_counts_and_source_revision_same(
    db_session, content_world
):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, _, target, accept = await moved_candidate(db, world)
    value = payload(world, recursive=False)
    before = await rules.preview(db, world[5].id, value, world[2])
    await revisions.accept(
        db, run.id, target["path_digest"], accept, world[2], relocate=True
    )
    after = await rules.preview(db, world[5].id, value, world[2])
    assert (
        before["total"] == after["total"] == 1
        and before["expected_revision"] == after["expected_revision"]
    )
    assert before["proof"] != after["proof"]
    with pytest.raises(HTTPException) as exc:
        await apply(db, world, value, before)
    assert exc.value.detail["code"] == "rules_impact_changed"


async def test_paging_and_reentry_preserve_binding_identity(db_session, content_world):
    db, world = db_session, content_world
    for name in ["b.png", "c.png"]:
        (world[0] / name).write_bytes(picture("blue"))
    world = await rescan(db, world)
    await accept_all_files(db, world)
    value = payload(world, include_globs=["*.jpg"])
    first = await rules.preview(db, world[5].id, value, world[2], limit=1)
    second = await rules.preview(db, world[5].id, value, world[2], limit=1, offset=1)
    assert first["proof"] == second["proof"] and first["total"] == 3
    assert first["items"][0]["path_digest"] != second["items"][0]["path_digest"]
    await apply(db, world, value, first)
    reopen = payload(world)
    impact = await rules.preview(db, world[5].id, reopen, world[2])
    assert impact["counts"]["entering_scope"] == 3
    await apply(db, world, reopen, impact)
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 3


async def test_active_attempt_requires_new_preview_after_cancel(
    db_session, content_world
):
    from app.services import dataset_scans as scans

    db, world = db_session, content_world
    run, _ = await scans.start_scan(db, world[5].id, world[2])
    await db.commit()
    value = payload(world, recursive=False)
    impact = await rules.preview(db, world[5].id, value, world[2])
    assert impact["active_attempt_count"] == 1
    with pytest.raises(HTTPException) as exc:
        await apply(db, world, value, impact)
    assert exc.value.detail["code"] == "source_busy"
    await scans.cancel_job(db, run.async_job_id, world[2])
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await apply(db, world, value, impact)
    assert exc.value.detail["code"] == "rules_impact_changed"


@pytest.mark.parametrize("key", ["relative_path", "node_id", "unknown_locator"])
async def test_reader_still_rejects_locator_and_unknown_configuration_changes(
    db_session, reference_world, httpx_client, annotator, key
):
    old, _, task, world = reference_world
    value = payload(world, exclude_globs=["*.png"])
    impact = await rules.preview(db_session, world[5].id, value, world[2])
    await apply(db_session, world, value, impact)
    world[5].config = {**world[5].config, key: "changed"}
    world[5].config_revision += 1
    await db_session.commit()
    r = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content",
        params={"sha256": old["sha256"]},
        headers=headers(annotator),
    )
    assert r.status_code == 409


async def test_rules_api_validation_scopes_and_revision(
    httpx_client, super_admin, annotator, content_world
):
    world = content_world
    url = f"/api/v1/dataset-sources/{world[5].id}/rules/preview"
    value = payload(world)
    assert (
        await httpx_client.post(
            url, json=value.model_dump(), headers=headers(annotator)
        )
    ).status_code == 403
    assert (
        await httpx_client.post(
            url,
            json={**value.model_dump(), "relative_path": "elsewhere"},
            headers=headers(super_admin),
        )
    ).status_code == 422
    assert (
        await httpx_client.post(
            url,
            json={**value.model_dump(), "expected_revision": 99},
            headers=headers(super_admin),
        )
    ).status_code == 409
    assert (
        await httpx_client.post(
            url, json=value.model_dump(), headers=headers(super_admin)
        )
    ).status_code == 200


@pytest.mark.parametrize(
    "backend,recursive,exclude,path,expected",
    [
        ("filesystem", True, ["dir"], "dir/a.png", False),
        ("sftp", True, ["dir"], "dir/a.png", False),
        ("s3", True, ["dir"], "dir/a.png", True),
        ("s3", True, ["dir/*"], "dir//a%2F+.png", False),
        ("filesystem", False, [], "dir/a.png", False),
        ("s3", False, [], "dir/a.png", False),
        ("s3", True, [], "a//b%2F+.png", True),
        ("filesystem", True, [], "DIR/a.PNG", False),
    ],
)
def test_exact_backend_selection_semantics(backend, recursive, exclude, path, expected):
    assert (
        selected_file(
            {
                "recursive": recursive,
                "include_globs": ["*.png"],
                "exclude_globs": exclude,
            },
            path,
            backend,
        )
        == expected
    )


def test_rules_reject_invalid_filters_and_locator_mutation():
    for fields in [
        {"include_globs": [""]},
        {"include_globs": ["a\x00b"]},
        {"relative_path": "elsewhere"},
    ]:
        with pytest.raises(ValidationError):
            Preview(
                **{
                    **dict(expected_revision=1, recursive=True, include_globs=["*"]),
                    **fields,
                }
            )


@pytest.mark.parametrize("rotate_at", [1, 2])
async def test_key_rotation_while_acquiring_source_lock_rejects_rule_apply(
    db_session, content_world, monkeypatch, rotate_at
):
    from app.db.models.api_key import ApiKey

    db, world = db_session, content_world
    key = ApiKey(
        user_id=world[2].id,
        name="rules",
        key_prefix="ak_rules",
        key_hash="original",
        scopes=["datasets:write"],
    )
    db.add(key)
    await db.flush()
    value = payload(world, recursive=False)
    impact = await rules.preview(db, world[5].id, value, world[2], api_key_id=key.id)
    original = rules.scans.source_context
    calls = 0

    async def rotate(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == rotate_at:
            key.key_hash = "rotated"
            await db.flush()
        return result

    monkeypatch.setattr(rules.scans, "source_context", rotate)
    with pytest.raises(HTTPException) as exc:
        await rules.apply(
            db,
            world[5].id,
            Apply(**value.model_dump(), impact_proof=impact["proof"]),
            world[2],
            key.id,
        )
    assert (
        exc.value.status_code == 403
        and world[5].config_revision == value.expected_revision
    )
