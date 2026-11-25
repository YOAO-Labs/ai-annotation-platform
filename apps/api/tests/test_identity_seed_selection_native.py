"""Old-behavior replay for Identity selection in operational seed consumers."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
import uuid

import pytest
from sqlalchemy import event, text

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.identity_commands import IdentityCommandService
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = ROOT / "apps/api/tests/fixtures/identity_seed_selection_legacy.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text())["snapshots"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@pytest.fixture
async def selection_world(test_engine):
    async with _world(test_engine) as (owner, consumer, role, _url):
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/identity-grants.sql",
            role,
            capability="scale_seed",
        )
        assert result.returncode == 0, result.stderr
        result = await _grants(
            test_engine,
            ROOT / "infra/docker/workspace-grants.sql",
            role,
            capability="scale_seed",
            assets_owner="assets_code",
            labeling_owner="labeling_code",
        )
        assert result.returncode == 0, result.stderr
        async with consumer() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
        yield owner, consumer


async def _scale_actors(owner, name, scenario):
    ids = {
        key: uuid.uuid4()
        for key in (
            "admin",
            "later_admin",
            "annotator",
            "later_annotator",
            "inactive",
            "reviewer",
            "unlinked",
        )
    }
    async with owner.begin() as db:
        for key, role, active, year in (
            (
                "admin",
                "viewer" if scenario == "no_admin" else "super_admin",
                False,
                2001,
            ),
            (
                "later_admin",
                "viewer" if scenario == "no_admin" else "super_admin",
                True,
                2001 if scenario == "ties" else 2002,
            ),
            ("annotator", "viewer", scenario != "inactive_only", 2003),
            (
                "later_annotator",
                "employee",
                scenario != "inactive_only",
                2003 if scenario == "ties" else 2004,
            ),
            ("inactive", "employee", False, 1990),
            ("reviewer", "employee", True, 1991),
            ("unlinked", "employee", True, 1980),
        ):
            db.add(
                User(
                    id=ids[key],
                    email=f"{name}-{key}@seed.test",
                    name=key,
                    password_hash="owned-seed-fixture",
                    role=role,
                    is_active=active,
                )
            )
            await db.flush()
            await db.execute(
                text(
                    f"UPDATE public.users SET created_at='{year}-01-01T00:00:00Z' WHERE id=:id"
                ),
                {"id": ids[key]},
            )
        projects = []
        for n in range(2 if scenario in {"cross_project", "duplicates"} else 1):
            project = Project(
                display_id="P-" + uuid.uuid4().hex[:12],
                name=f"support-{name}-{n}",
                type_key="image-det",
                type_label="fixture",
                owner_id=ids["later_admin"],
            )
            db.add(project)
            await db.flush()
            projects.append(project)
        for key in ("annotator", "later_annotator", "inactive", "reviewer"):
            db.add(
                ProjectMember(
                    project_id=projects[0].id,
                    user_id=ids[key],
                    role="reviewer"
                    if key == "reviewer" or scenario == "no_duty"
                    else "annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        if scenario == "cross_project":
            db.add(
                ProjectMember(
                    project_id=projects[1].id,
                    user_id=ids["unlinked"],
                    role="annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        elif scenario == "duplicates":
            db.add(
                ProjectMember(
                    project_id=projects[1].id,
                    user_id=ids["annotator"],
                    role="annotator",
                    assigned_by=ids["later_admin"],
                )
            )
        if scenario == "infinite":
            await db.execute(
                text(
                    "UPDATE public.users SET created_at='-infinity'::timestamptz WHERE id IN (:admin,:annotator)"
                ),
                {"admin": ids["admin"], "annotator": ids["annotator"]},
            )
    return ids


async def _remove_scale_actors(owner, name, ids):
    async with owner() as cleanup:
        project_ids = list(
            await cleanup.scalars(
                text(
                    "SELECT id FROM projects WHERE owner_id=:owner AND name=ANY(CAST(:names AS text[]))"
                ),
                {
                    "owner": ids["later_admin"],
                    "names": [f"support-{name}-0", f"support-{name}-1"],
                },
            )
        )
        await cleanup.execute(
            text(
                "DELETE FROM project_members WHERE project_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": project_ids},
        )
        await cleanup.execute(
            text("DELETE FROM projects WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": project_ids},
        )
        await cleanup.execute(
            text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": list(ids.values())},
        )
        await cleanup.commit()


async def test_node_selectors_are_read_only_and_fenced(selection_world):
    owner, consumer = selection_world
    name = "node-selection"
    ids = await _scale_actors(owner, name, "normal")
    try:
        url = (
            consumer.kw["bind"]
            .url.set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        result = subprocess.run(
            [
                "node",
                str(ROOT / "apps/api/tests/fixtures/identity_seed_selection_peer.mjs"),
            ],
            input=json.dumps(
                {
                    "database_url": url,
                    "admin_id": str(ids["admin"]),
                    "annotator_id": str(ids["annotator"]),
                }
            ),
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "ok": True,
            "read_only": True,
            "private_user_denied": True,
        }
    finally:
        await _remove_scale_actors(owner, name, ids)


async def test_combined_selector_uses_statement_start_snapshot(
    test_engine, selection_world, record_property
):
    owner, consumer = selection_world
    name = "snapshot-selection"
    ids = await _scale_actors(owner, name, "normal")
    key = uuid.uuid4().int % 2147483647
    tag = "seed-snapshot-" + uuid.uuid4().hex
    target = consumer.kw["bind"].sync_engine

    def gate(_connection, _cursor, statement, parameters, _context, _many):
        if "read_scale_seed_annotator" in statement:
            assert "yoao_workspace_v1.project_member_facts" in statement
            statement = (
                f"WITH aap_seed_gate AS MATERIALIZED (SELECT pg_catalog.pg_advisory_lock({key})) "
                + statement
                + " FROM aap_seed_gate"
            )
        return statement, parameters

    event.listen(target, "before_cursor_execute", gate, retval=True)
    task = None
    try:
        async with test_engine.connect() as lock:
            await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                async with consumer() as db:
                    await db.execute(
                        text("SELECT set_config('application_name', :tag, true)"),
                        {"tag": tag},
                    )
                    task = asyncio.create_task(
                        IdentityCommandService(db).resolve_scale_seed_actors()
                    )
                    deadline = asyncio.get_running_loop().time() + 10
                    while asyncio.get_running_loop().time() < deadline:
                        async with owner() as observer:
                            waiting = await observer.scalar(
                                text(
                                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND application_name=:tag AND wait_event_type='Lock' AND query LIKE 'WITH aap_seed_gate%'"
                                ),
                                {"tag": tag},
                            )
                        if waiting is not None:
                            break
                        if task.done():
                            pytest.fail(
                                f"selector exited before the owned statement gate: {task.result()}"
                            )
                        await asyncio.sleep(0.02)
                    else:
                        pytest.fail("selector did not reach the owned statement gate")
                    async with owner.begin() as external:
                        await external.execute(
                            text(
                                "UPDATE public.users SET is_active=false WHERE id=:id"
                            ),
                            {"id": ids["annotator"]},
                        )
                        await external.execute(
                            text(
                                "UPDATE project_members SET role='reviewer' WHERE user_id=:id"
                            ),
                            {"id": ids["annotator"]},
                        )
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
                    assert await asyncio.wait_for(asyncio.shield(task), 15) == (
                        ids["admin"],
                        ids["annotator"],
                    )
                    event.remove(target, "before_cursor_execute", gate)
                    assert await IdentityCommandService(
                        db
                    ).resolve_scale_seed_actors() == (
                        ids["admin"],
                        ids["later_annotator"],
                    )
                    record_property(
                        "scale_seed_statement_snapshot",
                        {
                            "backend_pid": waiting,
                            "role": consumer.kw["bind"].url.username,
                            "statement_start_candidate": str(ids["annotator"]),
                            "next_statement_candidate": str(ids["later_annotator"]),
                            "membership_and_activity_changed_together": True,
                        },
                    )
            finally:
                await lock.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), 15)
    finally:
        if event.contains(target, "before_cursor_execute", gate):
            event.remove(target, "before_cursor_execute", gate)
        await _remove_scale_actors(owner, name, ids)


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "no_admin",
        "no_duty",
        "inactive_only",
        "cross_project",
        "duplicates",
        "ties",
        "infinite",
    ],
)
async def test_scale_seed_selection_behavior(
    selection_world, scenario, record_property
):
    owner, consumer = selection_world
    name = "selection-" + scenario + "-" + uuid.uuid4().hex[:8]
    ids = await _scale_actors(owner, name, scenario)
    try:
        async with consumer() as db:
            selected = await IdentityCommandService(db).resolve_scale_seed_actors()
        labels = []
        for value in selected:
            label = next(
                (key for key, identifier in ids.items() if identifier == value), None
            )
            if scenario == "ties" and label in {"admin", "later_admin"}:
                label = "admin_tie"
            if scenario == "ties" and label in {"annotator", "later_annotator"}:
                label = "annotator_tie"
            labels.append(label)
        assert labels == GOLDEN["selection-" + scenario]["actors"]
        record_property("identity_scale_actor_selection", scenario)
    finally:
        await _remove_scale_actors(owner, name, ids)
