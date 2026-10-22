"""Replay parent seed behavior using fenced Identity/Workspace credentials."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import subprocess

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import hash_password, verify_password
from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from scripts import seed
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_workspace_consumer_profile_native import _world, _grants
from sqlalchemy.orm import aliased
from sqlalchemy.orm.exc import StaleDataError
from app.services.workspace_read_relations import project_member_facts
from app.services.seed_membership_data import SeedMembershipWrites
from aap_workspace.seed_member_contract import SeedMemberWriteUnavailable

ROOT = Path(__file__).resolve().parents[3]
OLD = datetime(2000, 1, 1, tzinfo=UTC)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned PostgreSQL and fenced operator credentials",
    ),
]


GOLDEN = json.loads(
    (ROOT / "apps/api/tests/fixtures/seed_parent_operator_legacy.json").read_text()
)


def _capture(name, value, record_property):
    assert value == GOLDEN["snapshots"][name]
    record_property("seed_parent_replay", name)


@pytest.fixture
async def parent_world(test_engine):
    async with _world(test_engine) as (owner, operator, role, _url):
        for filename in (
            "identity-seed-operator-grants.sql",
            "workspace-seed-operator-grants.sql",
        ):
            result = await _grants(test_engine, ROOT / "infra/docker" / filename, role)
            assert result.returncode == 0, result.stderr
        async with operator() as db:
            for table in ("users", "projects", "project_members"):
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user,:table,'SELECT,INSERT,UPDATE,DELETE') "
                        "OR has_any_column_privilege(current_user,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"table": "public." + table},
                )
        yield owner, operator


async def test_node_seed_producer_uses_both_owner_protocols(parent_world):
    owner, operator = parent_world
    async with owner.begin() as db:
        actor = User(
            id=uuid.uuid4(),
            email=f"peer-{uuid.uuid4()}",
            name="peer",
            role="employee",
            password_hash="test",
        )
        db.add(actor)
        await db.flush()
        project = Project(
            id=uuid.uuid4(),
            display_id="P-PEER-" + uuid.uuid4().hex[:8],
            name="peer",
            type_key="image-det",
            type_label="图像目标检测",
            owner_id=actor.id,
        )
        db.add(project)
        await db.flush()
        actor_id, project_id = actor.id, project.id
    url = (
        operator.kw["bind"]
        .url.set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    process = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/seed_parent_operator_peer.mjs")],
        input=json.dumps(
            {
                "database_url": url,
                "actor_id": str(actor_id),
                "project_id": str(project_id),
                "password_hash": hash_password("Node-Seed-1234"),
            }
        ),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "ok": True,
        "capabilities": 2,
        "joint_rollback": True,
    }


@pytest.mark.parametrize("failure", ["stale", "invalid_reply"])
@pytest.mark.parametrize("flush_phase", ["commit", "before_read"])
async def test_failed_seed_flush_cannot_commit_partial_native_work(
    test_engine, parent_world, monkeypatch, failure, flush_phase
):
    owner, operator = parent_world
    suffix = uuid.uuid4().hex[:8]
    async with owner.begin() as db:
        users = [
            User(
                id=uuid.uuid4(),
                email=f"atomic-{suffix}-{n}",
                name="atomic",
                role="employee",
                password_hash="test",
            )
            for n in range(3)
        ]
        db.add_all(users)
        await db.flush()
        project = Project(
            id=uuid.uuid4(),
            display_id=f"P-AM-{suffix}",
            name="atomic",
            type_key="image-det",
            type_label="图像目标检测",
            owner_id=users[0].id,
        )
        db.add(project)
        await db.flush()
        ids = [uuid.uuid4(), uuid.uuid4()]
        for member_id, user in zip(ids, users[:2], strict=True):
            db.add(
                ProjectMember(
                    id=member_id,
                    project_id=project.id,
                    user_id=user.id,
                    role="viewer",
                    assigned_by=users[0].id,
                )
            )
        project_id, actor_id, new_user_id = project.id, users[0].id, users[2].id
    async with operator(autoflush=flush_phase == "before_read") as db:
        row = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
        members = list(
            (
                await db.execute(
                    select(row).where(row.id.in_(ids)).order_by(row.user_id)
                )
            ).scalars()
        )
        writes = SeedMembershipWrites(db)
        for member in members:
            writes.change(member, role="annotator", assigned_by=actor_id)
        writes.add(
            project_id=project_id,
            user_id=new_user_id,
            role="reviewer",
            assigned_by=actor_id,
        )
        db.add(
            Dataset(
                display_id=f"D-AM-{suffix}",
                name="caller work",
                created_by=actor_id,
                data_type="image",
            )
        )
        if failure == "stale":
            removed_id = members[-1].id
            async with test_engine.begin() as external:
                await external.execute(
                    text("DELETE FROM project_members WHERE id=:id"), {"id": removed_id}
                )
            error = StaleDataError
        else:
            actual = db.sync_session.scalar

            def invalid_reply(statement, params):
                reply = actual(statement, params)
                assert reply["results"], (
                    "the real owner write must run before corrupting its receipt"
                )
                return {**reply, "unexpected": True}

            monkeypatch.setattr(db.sync_session, "scalar", invalid_reply)
            error = SeedMemberWriteUnavailable
        with pytest.raises(error):
            if flush_phase == "before_read":
                await writes.flush_before_read()
            else:
                await writes.commit()
        # Catching the bridge error cannot commit a successful earlier row or
        # the caller's pending work from the failed flush.
        await db.commit()
    async with test_engine.connect() as observer:
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM project_members WHERE project_id=:id AND role<>'viewer'"
                ),
                {"id": project_id},
            )
            == 0
        )
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM datasets WHERE display_id=:id"),
                {"id": f"D-AM-{suffix}"},
            )
            == 0
        )


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        "create",
        "equal",
        "repair",
        "missing_assigner",
        "missing_persona",
        "missing_project",
        "duplicate_new",
        "roundtrip_existing",
    ],
)
async def test_capture_memberships(
    test_engine, case, autoflush, record_property, parent_world
):
    _verify_postgres(test_engine.url)
    owner_maker, maker = parent_world
    suffix = uuid.uuid4().hex[:8]
    emails = {key: f"{key}-{suffix}" for key in ("pm", "admin", "anno", "qa")}
    users = {}
    display_ids = (f"P-DM-{suffix}-0", f"P-DM-{suffix}-1")
    projects = []
    async with owner_maker.begin() as db:
        for key in emails:
            if (case == "missing_assigner" and key in {"pm", "admin"}) or (
                case == "missing_persona" and key == "qa"
            ):
                continue
            user = User(
                id=uuid.uuid4(),
                email=emails[key],
                name=key,
                password_hash="test",
                role="viewer" if key == "pm" else "employee",
                is_active=False,
            )
            db.add(user)
            users[key] = user.id
        await db.flush()
        owner_id = users.get("admin", users["anno"])
        for display_id in display_ids:
            project = Project(
                id=uuid.uuid4(),
                display_id=display_id,
                name=display_id,
                type_key="image-det",
                type_label="图像目标检测",
                owner_id=owner_id,
            )
            db.add(project)
            projects.append(project.id)
        await db.flush()
        if case in {"equal", "repair", "roundtrip_existing"}:
            for index, project_id in enumerate(projects):
                for key, role in (("anno", "annotator"), ("qa", "reviewer")):
                    if case == "roundtrip_existing" and key == "qa":
                        continue
                    db.add(
                        ProjectMember(
                            id=uuid.uuid4(),
                            project_id=project_id,
                            user_id=users[key],
                            role="viewer"
                            if case == "repair" and index == 0 and key == "anno"
                            else role,
                            assigned_by=users["pm"]
                            if case == "roundtrip_existing"
                            else users["admin"],
                            assigned_at=OLD,
                            updated_at=OLD,
                            version=7,
                            weekly_target=99,
                        )
                    )
    members = ((emails["anno"], "annotator"), (emails["qa"], "reviewer"))
    if case == "duplicate_new":
        members = ((emails["anno"], "annotator"), (emails["anno"], "reviewer"))
    if case == "roundtrip_existing":
        members = ((emails["anno"], "reviewer"), (emails["anno"], "annotator"))
    commits, commit_attempts, project_reads = [], [], []

    def executed(_conn, _cursor, sql, _params, _context, _many):
        if re.search(
            r"\bfrom\s+(?:public\.)?projects\b|aap_workspace_v1\.project_facts",
            sql,
            re.I,
        ):
            project_reads.append(sql)

    async with maker(autoflush=autoflush) as db:
        pending = Dataset(
            display_id=f"D-DM-{suffix}",
            name="caller pending",
            created_by=owner_id,
            data_type="image",
        )
        db.add(pending)
        event.listen(
            db.sync_session, "after_commit", lambda session: commits.append(True)
        )
        event.listen(
            db.sync_session,
            "before_commit",
            lambda session: commit_attempts.append(True),
        )
        event.listen(maker.kw["bind"].sync_engine, "before_cursor_execute", executed)
        changed, error = None, None
        try:
            changed = await seed._ensure_demo_memberships(
                db,
                members=members,
                display_ids=("P-DM-not-present",)
                if case == "missing_project"
                else display_ids,
                assigner_emails=(emails["pm"], emails["admin"]),
            )
        except IntegrityError as exc:
            if case != "duplicate_new" or autoflush:
                raise
            error = exc.orig.sqlstate
            assert error == "23505"
        finally:
            event.remove(
                maker.kw["bind"].sync_engine, "before_cursor_execute", executed
            )
        materialized = pending.__dict__.get("id") is not None
        await db.rollback()

    async with test_engine.connect() as observer:
        rows = (
            (
                await observer.execute(
                    text(
                        "SELECT p.display_id,u.email,m.role,a.email AS assigner,m.version,m.weekly_target,"
                        "m.assigned_at=:old AS assigned_at_old,m.updated_at=:old AS updated_at_old "
                        "FROM project_members m JOIN projects p ON p.id=m.project_id "
                        "JOIN users u ON u.id=m.user_id LEFT JOIN users a ON a.id=m.assigned_by "
                        "WHERE m.project_id=ANY(CAST(:ids AS uuid[])) ORDER BY p.display_id,u.email"
                    ),
                    {"old": OLD, "ids": projects},
                )
            )
            .mappings()
            .all()
        )
        pending_visible = (
            await observer.scalar(
                text("SELECT count(*) FROM datasets WHERE display_id=:id"),
                {"id": f"D-DM-{suffix}"},
            )
            == 1
        )
    values = []
    for row in rows:
        value = dict(row)
        value["project"] = display_ids.index(value.pop("display_id"))
        value["email"] = value["email"].removesuffix("-" + suffix)
        value["assigner"] = value["assigner"].removesuffix("-" + suffix)
        values.append(value)
    assert len(commits) == int(changed is not None and changed > 0)
    assert not project_reads if case == "missing_assigner" else project_reads
    _capture(
        f"memberships-{case}-{autoflush}",
        {
            "case": case,
            "autoflush": autoflush,
            "changed": changed,
            "error_sqlstate": error,
            "commits": len(commits),
            "commit_attempts": len(commit_attempts),
            "pending_id_materialized": materialized,
            "pending_committed": pending_visible,
            "project_queries": len(project_reads),
            "rows": values,
        },
        record_property,
    )


async def test_capture_parent_accounts(
    test_engine, monkeypatch, record_property, parent_world
):
    _verify_postgres(test_engine.url)
    owner_maker, operator = parent_world
    original_specs = [dict(user) for user in seed.USERS]
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM users WHERE email=ANY(CAST(:emails AS text[]))"
                ),
                {"emails": [u["email"] for u in original_specs]},
            )
            == 0
        )
    for case, autoflush in (("new", True), ("reuse_pm", False)):
        maker = async_sessionmaker(
            operator.kw["bind"], expire_on_commit=False, autoflush=autoflush
        )
        specs = list(original_specs)
        old_hash = None
        if case == "reuse_pm":
            old_hash = hash_password("Keep-Existing-1234")
            async with owner_maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE users SET name='legacy pm',role='viewer',group_name='legacy group',"
                        "is_active=false,password_hash=:hash WHERE email='pm'"
                    ),
                    {"hash": old_hash},
                )
            specs.append(
                {
                    "email": "seed-extra",
                    "name": "extra",
                    "password": "123456",
                    "role": "employee",
                    "group_name": "capture group",
                }
            )
        monkeypatch.setattr(seed, "USERS", specs)
        monkeypatch.setattr(seed, "Session", maker)
        dispose = AsyncMock()
        monkeypatch.setattr(seed, "engine", SimpleNamespace(dispose=dispose))
        monkeypatch.setattr(seed, "seed_coco8", AsyncMock(return_value=None))
        monkeypatch.setattr(seed, "seed_video", AsyncMock(return_value=None))
        monkeypatch.setattr(
            seed,
            "seed_nuscenes_scene",
            AsyncMock(
                return_value=({"scenes": [], "total_items": 0, "batches": 0}, [])
            ),
        )
        await seed.seed(profile="demo", offline=True)
        dispose.assert_awaited_once()
        async with test_engine.connect() as db:
            rows = (
                (
                    await db.execute(
                        text(
                            "SELECT to_jsonb(u) AS state FROM users u WHERE email=ANY(CAST(:emails AS text[])) ORDER BY email"
                        ),
                        {"emails": [u["email"] for u in specs]},
                    )
                )
                .scalars()
                .all()
            )
        states = []
        for row in rows:
            value = dict(row)
            stored_hash = value.pop("password_hash")
            value.pop("id")
            for field in ("created_at", "updated_at"):
                value.pop(field, None)
            if case == "reuse_pm" and value["email"] == "pm":
                assert stored_hash == old_hash
                value["password_preserved"] = True
            else:
                assert verify_password("123456", stored_hash)
                value["password_matches_seed"] = True
            states.append(value)
        assert len(states) == len(specs)
        _capture(
            f"accounts-{case}-{autoflush}",
            {
                "case": case,
                "autoflush": autoflush,
                "states": states,
                "scope": "actual parent seed; media child entrypoints isolated",
            },
            record_property,
        )


@pytest.mark.parametrize(
    "case",
    ["dirty_repair_back", "dirty_noop", "partial_expiry", "changed", "stale_cached"],
)
async def test_capture_membership_cache(
    test_engine, case, record_property, parent_world
):
    _verify_postgres(test_engine.url)
    owner_maker, maker = parent_world
    suffix = uuid.uuid4().hex[:8]
    pm_email, target_email = f"cpm-{suffix}", f"canno-{suffix}"
    async with owner_maker.begin() as db:
        pm = User(
            id=uuid.uuid4(),
            email=pm_email,
            name="pm",
            role="viewer",
            password_hash="test",
        )
        target = User(
            id=uuid.uuid4(),
            email=target_email,
            name="anno",
            role="employee",
            password_hash="test",
        )
        db.add_all([pm, target])
        await db.flush()
        project = Project(
            id=uuid.uuid4(),
            display_id=f"P-CM-{suffix}",
            name="cache",
            type_key="image-det",
            type_label="图像目标检测",
            owner_id=pm.id,
        )
        db.add(project)
        await db.flush()
        member = ProjectMember(
            id=uuid.uuid4(),
            project_id=project.id,
            user_id=target.id,
            role="annotator",
            assigned_by=pm.id,
            assigned_at=OLD,
            updated_at=OLD,
            version=7,
            weekly_target=99,
        )
        db.add(member)
        member_id, display_id = member.id, project.display_id
    async with maker(autoflush=False) as db:
        member_row = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
        cached = await db.scalar(select(member_row).where(member_row.id == member_id))
        if case.startswith("dirty_"):
            cached.role = "reviewer"
        if case == "partial_expiry":
            db.expire(cached, ["role", "assigned_by", "updated_at"])
        if case == "stale_cached":
            async with test_engine.begin() as external:
                await external.execute(
                    text(
                        "UPDATE project_members SET role='viewer',updated_at='2010-01-01T00:00:00Z' WHERE id=:id"
                    ),
                    {"id": member_id},
                )
        role = "reviewer" if case in {"dirty_noop", "changed"} else "annotator"
        changed = await seed._ensure_demo_memberships(
            db,
            members=((target_email, role),),
            display_ids=(display_id,),
            assigner_emails=(pm_email,),
        )
        state = inspect(cached)
        cache = {
            "role": cached.role,
            "role_dirty": state.attrs.role.history.has_changes(),
            "updated_at_unloaded": "updated_at" in state.unloaded,
            "updated_at_old": cached.__dict__.get("updated_at") == OLD,
            "assigned_by_loaded": "assigned_by" not in state.unloaded,
            "version": cached.version,
            "weekly_target": cached.weekly_target,
        }
        async with test_engine.connect() as observer:
            persisted = (
                (
                    await observer.execute(
                        text(
                            "SELECT role, version, weekly_target, assigned_at=:old AS assigned_at_old,updated_at=:old AS updated_at_old "
                            "FROM project_members WHERE id=:id"
                        ),
                        {"id": member_id, "old": OLD},
                    )
                )
                .mappings()
                .one()
            )
        await db.rollback()
    _capture(
        f"cache-{case}",
        {
            "case": case,
            "changed": changed,
            "cache": cache,
            "persisted": dict(persisted),
        },
        record_property,
    )
