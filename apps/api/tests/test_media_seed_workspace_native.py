"""Replay actual media seed Project behavior with the private table fenced."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from PIL import Image
import pytest
from sqlalchemy import event, text

from app.db.models.dataset import Dataset
from app.db.models.project import Project
from scripts import seed_coco8, seed_ocr, seed_video
from tests.factory import create_user
from tests.test_import_nuscenes_lite import _patch_storage_clients
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
LEGACY = os.getenv("AAP_WORKSPACE_MEDIA_LEGACY_CAPTURE") == "1"
GOLDEN = ROOT / "apps/api/tests/fixtures/workspace_media_seed_legacy.json"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


def _replay(name, value, record_property):
    record_property(
        "workspace_media_legacy", json.dumps({"case": name, "state": value})
    )
    if not LEGACY:
        assert value == json.loads(GOLDEN.read_text())["snapshots"][name]


@pytest.fixture
async def media_world(test_engine):
    async with _world(test_engine, apply=not LEGACY) as (owner, producer, role, _url):
        if not LEGACY:
            result = await _grants(
                test_engine,
                ROOT / "infra/docker/workspace-seed-operator-grants.sql",
                role,
            )
            assert result.returncode == 0, result.stderr
            # Other-domain seed operations keep their existing privileges; this
            # fixture measures only the Project boundary.
            async with test_engine.begin() as db:
                signatures = (
                    await db.scalars(
                        text(
                            "SELECT p.oid::regprocedure::text FROM pg_proc p "
                            "JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname IN ('aap_assets_v1','aap_labeling_v1','aap_jobs_v1') "
                            "AND has_function_privilege('legacy_runtime',p.oid,'EXECUTE')"
                        )
                    )
                ).all()
                for signature in signatures:
                    await db.execute(
                        text(f'GRANT EXECUTE ON FUNCTION {signature} TO "{role}"')
                    )
            async with producer() as db:
                for relation in ("projects", "project_members"):
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,DELETE') OR has_any_column_privilege(current_user,:relation,'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"relation": "public." + relation},
                    )
        yield owner, producer


@pytest.fixture
async def screenshot_world(media_world):
    from app.application.project_deletion import delete_project

    owner, _producer = media_world
    tables = ("projects", "datasets", "users")
    async with owner() as db:
        before = {
            table: set((await db.scalars(text(f"SELECT id FROM {table}"))).all())
            for table in tables
        }
    try:
        yield media_world
    finally:
        async with owner.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            for table in tables:
                added = (
                    set((await db.scalars(text(f"SELECT id FROM {table}"))).all())
                    - before[table]
                )
                if added:
                    if table == "projects":
                        for project_id in sorted(added):
                            actor_id = await db.scalar(
                                text("SELECT owner_id FROM projects WHERE id=:id"),
                                {"id": project_id},
                            )
                            await delete_project(db, project_id, actor_id=actor_id)
                    else:
                        await db.execute(
                            text(
                                f"DELETE FROM {table} WHERE id=ANY(CAST(:ids AS uuid[]))"
                            ),
                            {"ids": list(added)},
                        )


async def _state(owner, display_id):
    async with owner() as db:
        value = await db.scalar(
            text("SELECT to_jsonb(p) FROM public.projects p WHERE display_id=:id"),
            {"id": display_id},
        )
    assert value is not None
    for name in ("id", "owner_id", "display_id", "created_at", "updated_at"):
        value.pop(name)
    return value


@pytest.mark.parametrize("kind", ["coco8", "ocr", "video"])
async def test_fresh_media_seed_original_fields_and_resume(
    media_world, kind, monkeypatch, tmp_path, record_property
):
    owner, producer = media_world
    module = {"coco8": seed_coco8, "ocr": seed_ocr, "video": seed_video}[kind]
    display_id = "P-" + uuid.uuid4().hex[:12]
    dataset_id = "D-" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(module, "PROJECT_DISPLAY_ID", display_id)
    monkeypatch.setattr(module, "DATASET_DISPLAY_ID", dataset_id)
    fake = _patch_storage_clients(monkeypatch)
    async with owner.begin() as db:
        actor = await create_user(
            db, "super_admin", f"media-{uuid.uuid4()}@seed.test", "Media owner"
        )
        actor_id = actor.id
    if kind == "coco8":
        for split, name in (("train", "a"), ("val", "b")):
            images = tmp_path / "images" / split
            labels = tmp_path / "labels" / split
            images.mkdir(parents=True)
            labels.mkdir(parents=True)
            Image.new("RGB", (8, 6)).save(images / (name + ".jpg"))
            (labels / (name + ".txt")).write_text("0 0.5 0.5 0.5 0.5\n")
        options = {"fixture": tmp_path, "prediction_model_version": "frozen-test"}
    elif kind == "ocr":
        image = tmp_path / "ocr.jpg"
        Image.new("RGB", (8, 6)).save(image)
        options = {"image": image}
    else:
        video = ROOT / "docs-site/public/home/sam-tools/smart-point.webm"
        media_hash = hashlib.sha256(video.read_bytes()).hexdigest()
        options = {"video": video}
    async with producer() as db:
        result = await getattr(module, "seed_" + kind)(db, owner_id=actor_id, **options)
        await db.commit()
        repeated = await getattr(module, "seed_" + kind)(
            db, owner_id=actor_id, **options
        )
        await db.commit()
    assert result is not None
    if kind == "video":
        assert hashlib.sha256(video.read_bytes()).hexdigest() == media_hash
    state = await _state(owner, display_id)
    async with owner() as db:
        state["task_count"] = await db.scalar(
            text(
                "SELECT count(*) FROM tasks t JOIN projects p ON t.project_id=p.id WHERE p.display_id=:id"
            ),
            {"id": display_id},
        )
        state["prediction_count"] = await db.scalar(
            text(
                "SELECT count(*) FROM predictions t JOIN projects p ON t.project_id=p.id WHERE p.display_id=:id"
            ),
            {"id": display_id},
        )
    state["uploaded_object_count"] = len(fake.objects)
    state["resume_result"] = repeated
    if isinstance(repeated, dict):
        state["resume_result"] = {
            key: value for key, value in repeated.items() if key != "project"
        }
    _replay("fresh-" + kind, state, record_property)


@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_coco_existing_merge_keeps_transaction_and_cache(
    media_world, autoflush, commit, monkeypatch, tmp_path, record_property
):
    owner, producer = media_world
    display_id = "P-" + uuid.uuid4().hex[:12]
    pending_id = "D-" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(seed_coco8, "PROJECT_DISPLAY_ID", display_id)
    original = {"bbox": {"keep": True}, "region": {"custom": True}}
    async with owner.begin() as db:
        actor = await create_user(
            db, "super_admin", f"coco-{uuid.uuid4()}@seed.test", "Coco owner"
        )
        actor_id = actor.id
        db.add(
            Project(
                display_id=display_id,
                name="Keep this name",
                type_label="Keep",
                type_key="image-det",
                data_type="image",
                owner_id=actor_id,
                tool_bindings=original,
                updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
    async with producer(autoflush=autoflush) as db:
        pending = Dataset(
            display_id=pending_id, name="Caller pending", created_by=actor_id
        )
        db.add(pending)
        commits = []
        event.listen(
            db.sync_session, "after_commit", lambda _session: commits.append(True)
        )
        result = await seed_coco8.seed_coco8(
            db, owner_id=actor_id, fixture=tmp_path / "missing"
        )
        state = {
            "result": {key: value for key, value in result.items() if key != "project"},
            "pending_materialized": pending.id is not None,
            "commits_in_helper": len(commits),
        }
        async with owner() as observer:
            state["pending_visible_before_action"] = await observer.scalar(
                text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
                {"id": pending_id},
            )
        if commit:
            await db.commit()
        else:
            await db.rollback()
        state["commits_after_action"] = len(commits)
    state["project"] = await _state(owner, display_id)
    async with owner() as observer:
        state["pending_visible_after_action"] = await observer.scalar(
            text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
            {"id": pending_id},
        )
    _replay(f"coco-existing-{autoflush}-{commit}", state, record_property)


@pytest.mark.skipif(LEGACY, reason="new restricted consumer acceptance")
@pytest.mark.parametrize("autoflush", [True, False])
async def test_screenshot_reconcile_pins_workspace_and_member_state(
    screenshot_world, autoflush, monkeypatch
):
    from app.services.screenshot_seed_catalog import build_screenshot_seed_catalog
    from scripts.seed_screenshot_profile import (
        FIXED_TIME as SEED_FIXED_TIME,
        ScreenshotSeedPreparation,
        reconcile_screenshot_seed,
    )
    from app.services.screenshot_seed_spec import PROJECT_SPECS
    from tests.test_screenshot_seed_catalog import _ready_profile

    owner, producer = screenshot_world
    monkeypatch.setattr(
        "app.services.screenshot_seed_catalog._storage_object_digest",
        lambda path: hashlib.sha256(path.encode()).hexdigest(),
    )
    async with owner.begin() as db:
        projects, _tasks = await _ready_profile(db)
        ids = {key: project.id for key, project in projects.items()}
    digests = {key: "a" * 64 for key in PROJECT_SPECS}
    reports = []
    for _ in range(2):
        async with producer(autoflush=autoflush) as db:
            reports.append(
                await reconcile_screenshot_seed(
                    db,
                    preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
                    asset_sha256=digests,
                )
            )
            catalog = await build_screenshot_seed_catalog(
                db, backend_requirements="none"
            )
            assert set(catalog["projects"]) == set(PROJECT_SPECS)
        async with owner() as db:
            for project_id in ids.values():
                assert (
                    await db.execute(
                        text("SELECT created_at,updated_at FROM projects WHERE id=:id"),
                        {"id": project_id},
                    )
                ).one() == (SEED_FIXED_TIME, SEED_FIXED_TIME)
            members = (
                await db.execute(
                    text(
                        "SELECT role,assigned_at,version FROM project_members WHERE project_id=:id ORDER BY role"
                    ),
                    {"id": ids["image_demo"]},
                )
            ).all()
            assert [(row.role, row.assigned_at) for row in members] == [
                ("annotator", SEED_FIXED_TIME),
                ("reviewer", SEED_FIXED_TIME),
            ]
    assert reports == [{"projects": 5, "tasks": 50, "batches": 5}] * 2


@pytest.mark.skipif(LEGACY, reason="new restricted consumer acceptance")
async def test_screenshot_backend_project_fields_survive_intermediate_autoflush(
    screenshot_world, monkeypatch
):
    from sqlalchemy import select
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.services.screenshot_seed_backends import reconcile_screenshot_backends
    from tests.test_screenshot_seed_catalog import _ready_profile

    owner, producer = screenshot_world
    async with owner.begin() as db:
        await _ready_profile(db)

    async def candidates(db):
        return list((await db.scalars(select(MLBackendRegistry))).all())

    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._live_candidates", candidates
    )
    async with producer() as db:
        report = await reconcile_screenshot_backends(db, mode="live")
        assert set(report["bindings"]) == {"image_demo", "video_demo", "ocr_demo"}
    async with owner() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM projects WHERE ml_backend_pool_id IS NOT NULL"
                )
            )
            == 3
        )
    async with producer() as db:
        await reconcile_screenshot_backends(
            db, mode="live", backend_requirements="none"
        )
    async with owner() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM projects WHERE ml_backend_pool_id IS NOT NULL OR preannotate_pipeline IS DISTINCT FROM '[]'::jsonb"
                )
            )
            == 0
        )


@pytest.mark.skipif(LEGACY, reason="new restricted consumer acceptance")
async def test_seed_projection_missing_row_preserves_caller_rollback(media_world):
    from sqlalchemy import select
    from sqlalchemy.orm.exc import StaleDataError
    from app.services.workspace_seed_data import (
        register_seed_project_projection,
        seed_projects,
    )

    owner, producer = media_world
    async with owner.begin() as db:
        actor = await create_user(
            db, "super_admin", f"stale-{uuid.uuid4()}@seed.test", "Owner"
        )
        project = Project(
            display_id="P-" + uuid.uuid4().hex[:12],
            name="Original",
            type_label="Image",
            type_key="image-det",
            data_type="image",
            owner_id=actor.id,
        )
        db.add(project)
        await db.flush()
        project_id, actor_id = project.id, actor.id
    async with producer() as db:
        project = await db.scalar(
            select(seed_projects).where(seed_projects.id == project_id)
        )
        register_seed_project_projection(db, project_id)
        project.name = "Stale write"
        async with owner.begin() as observer:
            await observer.execute(
                text("DELETE FROM projects WHERE id=:id"), {"id": project_id}
            )
        pending_id = "D-" + uuid.uuid4().hex[:12]
        db.add(Dataset(display_id=pending_id, name="Rollback me", created_by=actor_id))
        with pytest.raises(StaleDataError):
            await db.flush()
        await db.rollback()
    async with owner() as observer:
        assert not await observer.scalar(
            text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
            {"id": pending_id},
        )


@pytest.mark.skipif(LEGACY, reason="new independent language consumer")
async def test_node_calls_media_and_member_contracts_without_python_provider(
    media_world, record_property
):
    owner, producer = media_world
    async with owner.begin() as db:
        actor = await create_user(
            db, "super_admin", f"node-media-{uuid.uuid4()}@seed.test", "Owner"
        )
        member = await create_user(
            db, "employee", f"node-member-{uuid.uuid4()}@seed.test", "Member"
        )
        actor_id, member_id = actor.id, member.id
    url = (
        producer.kw["bind"]
        .url.set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    process = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/workspace_media_seed_peer.mjs")],
        input=json.dumps(
            {
                "database_url": url,
                "owner_id": str(actor_id),
                "member_user_id": str(member_id),
            }
        ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result == {
        "ok": True,
        "media_kinds": 4,
        "new_functions": 3,
        "private_tables_denied": True,
        "rolled_back": True,
    }
    record_property("workspace_media_node", json.dumps(result))
