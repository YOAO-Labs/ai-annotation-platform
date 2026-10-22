"""Replay media seed Identity behavior against owner-denied consumer credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import aliased, load_only

from app.config import settings
from app.core.security import hash_password, verify_password
from app.db.models.dataset import Dataset, ProjectDataset
from app.db.models.project import Project
from app.db.models.user import User
from app.services import screenshot_seed_catalog as catalog
from app.services.identity_read_relations import account_profiles
from scripts import (
    seed_coco8,
    seed_large_images,
    seed_ocr,
    seed_screenshot_profile,
    seed_video,
)
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = ROOT / "apps/api/tests/fixtures/media_seed_identity_legacy.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text())["snapshots"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the explicitly owned module-data verifier",
    ),
]


def _replay(name, value, record_property):
    assert value == GOLDEN[name]
    record_property("media_seed_identity_replay", name)


@pytest.fixture
async def media_world(test_engine):
    async with _world(test_engine, apply=False) as (owner, producer, role, _url):
        result = await _grants(
            test_engine, ROOT / "infra/docker/identity-seed-operator-grants.sql", role
        )
        assert result.returncode == 0, result.stderr
        async with producer() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.users',"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                    "OR has_any_column_privilege(current_user,'public.users','SELECT,INSERT,UPDATE,REFERENCES')"
                )
            )
        yield owner, producer


async def _actor(owner, email, *, role="employee", active=True, **fields):
    async with owner.begin() as db:
        user = User(
            id=uuid.uuid4(),
            email=email,
            name="legacy account",
            password_hash=hash_password("keep-existing"),
            role=role,
            is_active=active,
            **fields,
        )
        db.add(user)
        await db.flush()
        return user.id, user.password_hash


async def _full_account(owner, email, *, original_hash=None):
    async with owner() as db:
        raw = await db.scalar(
            text(
                "SELECT to_jsonb(account) FROM public.users AS account WHERE email=:email"
            ),
            {"email": email},
        )
    if raw is None:
        return None
    raw.pop("id")
    raw.pop("created_at")
    password = raw.pop("password_hash")
    raw["seed_password_matches"] = verify_password("123456", password)
    raw["existing_hash_preserved"] = (
        original_hash is not None and password == original_hash
    )
    return raw


def _dataset_display(name):
    return "D-" + uuid.uuid5(uuid.NAMESPACE_URL, name).hex[:12]


def _commits(db):
    counts = [0]

    def after_commit(_session):
        counts[0] += 1

    event.listen(db.sync_session, "after_commit", after_commit)
    return counts


@pytest.mark.parametrize(
    "module", [seed_coco8, seed_video, seed_ocr], ids=["coco8", "video", "ocr"]
)
@pytest.mark.parametrize("autoflush", [True, False])
@pytest.mark.parametrize(
    "scenario", ["new_commit", "new_rollback", "existing_commit", "production"]
)
async def test_media_admin_behavior(
    media_world, module, autoflush, scenario, monkeypatch, record_property
):
    owner, producer = media_world
    name = f"admin-{module.__name__.split('.')[-1]}-{scenario}-{autoflush}"
    email = name + "@seed.test"
    actor_id, _ = await _actor(owner, name + "-caller@seed.test")
    original_hash = None
    existing_id = None
    if scenario == "existing_commit":
        existing_id, original_hash = await _actor(
            owner,
            email,
            role="viewer",
            active=False,
            group_name="保留分组",
            credential_generation=7,
            weekly_target_default=42,
            preferences={"keep": True},
        )
    monkeypatch.setattr(module, "ADMIN_EMAIL", email)
    async with producer(autoflush=autoflush) as db:
        commits = _commits(db)
        pending = Dataset(
            display_id=_dataset_display(name),
            name="caller pending",
            created_by=actor_id,
        )
        db.add(pending)
        with monkeypatch.context() as policy:
            policy.setattr(
                settings,
                "environment",
                "production" if scenario == "production" else "development",
            )
            account_id = await module._ensure_admin(db)
        pending_materialized = pending.id is not None
        profile = (
            (
                await db.execute(
                    text(
                        "SELECT email,name,role,is_active,status,avatar_ref FROM aap_identity_v1.account_profiles WHERE id=:id"
                    ),
                    {"id": account_id},
                )
            )
            .mappings()
            .one()
        )
        async with owner() as observer:
            before = await observer.scalar(
                text("SELECT EXISTS(SELECT 1 FROM public.users WHERE email=:email)"),
                {"email": email},
            )
            pending_before = await observer.scalar(
                text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
                {"id": pending.display_id},
            )
        state = {
            "return_is_uuid": isinstance(account_id, uuid.UUID),
            "existing_id_reused": existing_id is not None and account_id == existing_id,
            "profile": dict(profile),
            "pending_materialized": pending_materialized,
            "commits_during_helper": commits[0],
            "account_visible_before_caller_commit": before,
            "pending_visible_before_caller_commit": pending_before,
        }
        if scenario == "new_rollback":
            await db.rollback()
        else:
            await db.commit()
        state["commits_after_caller_action"] = commits[0]
    async with owner() as observer:
        state["pending_visible_after_caller_action"] = await observer.scalar(
            text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
            {"id": _dataset_display(name)},
        )
    state["account_after_caller_action"] = await _full_account(
        owner, email, original_hash=original_hash
    )
    _replay(name, state, record_property)


async def _read_fixture(owner, name, scenario):
    first_role = "viewer" if scenario in {"wrong_role", "cache_dirty"} else "employee"
    ids = {}
    for key, role in (("first", first_role), ("second", "viewer")):
        if scenario == "missing_" + key:
            continue
        ids[key], _ = await _actor(
            owner, f"{name}-{key}@seed.test", role=role, active=scenario != "inactive"
        )
    return ids


async def _cached_first(db, owner, name, scenario, ids):
    if not scenario.startswith("cache_"):
        return None
    profile = aliased(User, account_profiles, adapt_on_names=True)
    user = await db.scalar(
        select(profile)
        .options(load_only(profile.id, profile.email, profile.role, profile.is_active))
        .where(profile.id == ids["first"])
    )
    if scenario == "cache_dirty":
        user.role = "employee"
    elif scenario == "cache_email":
        user.email = "cached-email@seed.test"
    else:
        if scenario == "cache_expired":
            db.expire(user, ["role"])
        async with owner.begin() as external:
            await external.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": ids["first"]},
            )
    return user


def _read_counter(db):
    counts = {"identity": 0, "project": 0}

    def before_cursor(_conn, _cursor, statement, _parameters, _context, _many):
        sql = statement.lower()
        if "account_profiles" in sql or re.search(
            r"\bfrom\s+(?:public\.)?users\b", sql
        ):
            counts["identity"] += 1
        if re.search(r"\bfrom\s+(?:public\.)?projects\b", sql):
            counts["project"] += 1

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before_cursor)
    return counts, lambda: event.remove(engine, "before_cursor_execute", before_cursor)


_PROFILE_CASES = [
    (scenario, auto)
    for scenario in (
        "valid",
        "missing_first",
        "missing_second",
        "wrong_role",
        "cache_stale",
        "cache_expired",
    )
    for auto in (True, False)
] + [("cache_dirty", False)]
_CATALOG_CASES = [
    (scenario, auto)
    for scenario in ("valid", "missing_second", "wrong_role", "inactive", "cache_stale")
    for auto in (True, False)
] + [("cache_email", False)]


@pytest.mark.parametrize(
    ("scenario", "autoflush"), _PROFILE_CASES, ids=lambda v: str(v)
)
async def test_screenshot_persona_behavior(
    media_world, scenario, autoflush, monkeypatch, record_property
):
    await _read_case(
        media_world,
        scenario,
        autoflush,
        monkeypatch,
        record_property,
        catalog_mode=False,
    )


@pytest.mark.parametrize(
    ("scenario", "autoflush"), _CATALOG_CASES, ids=lambda v: str(v)
)
async def test_screenshot_catalog_persona_behavior(
    media_world, scenario, autoflush, monkeypatch, record_property
):
    await _read_case(
        media_world,
        scenario,
        autoflush,
        monkeypatch,
        record_property,
        catalog_mode=True,
    )


async def _read_case(
    media_world, scenario, autoflush, monkeypatch, record_property, *, catalog_mode
):
    owner, producer = media_world
    name = f"{'catalog' if catalog_mode else 'profile'}-{scenario}-{autoflush}"
    ids = await _read_fixture(owner, name, scenario)
    actor_id, _ = await _actor(owner, name + "-caller@seed.test")
    specs = {
        "first": (f"{name}-first@seed.test", "employee"),
        "second": (f"{name}-second@seed.test", "viewer"),
    }
    target = catalog if catalog_mode else seed_screenshot_profile
    monkeypatch.setattr(target, "USER_SPECS", specs)
    if catalog_mode:
        monkeypatch.setattr(catalog, "PROJECT_SPECS", {})
    async with producer(autoflush=autoflush) as db:
        cached = await _cached_first(db, owner, name, scenario, ids)
        pending = Dataset(
            display_id=_dataset_display(name),
            name="caller pending",
            created_by=actor_id,
        )
        db.add(pending)
        commits = _commits(db)
        counts, stop = _read_counter(db)
        try:
            if catalog_mode:
                result = await catalog.build_screenshot_seed_catalog(db)
                payload = result["users"]
                for key, user in payload.items():
                    user["id"] = "<" + key + ">"
                outcome = {"users": payload, "projects": result["projects"]}
            else:
                result = await seed_screenshot_profile._seed_users(db)
                outcome = {
                    "users": {
                        key: "<" + key + ">"
                        for key, user in result.items()
                        if (user if isinstance(user, uuid.UUID) else user.id)
                        == ids[key]
                    }
                }
        except catalog.ScreenshotSeedCatalogError as exc:
            outcome = {"error": type(exc).__name__, "issues": exc.issues}
        except seed_screenshot_profile.ScreenshotSeedReconcileError as exc:
            outcome = {"error": type(exc).__name__, "message": str(exc)}
        finally:
            stop()
        outcome.update(
            reads=counts,
            commits=commits[0],
            pending_materialized=pending.id is not None,
        )
        if cached is not None:
            outcome["cached_dirty"] = cached in db.dirty
    async with owner() as observer:
        outcome["pending_visible_after_close"] = await observer.scalar(
            text("SELECT EXISTS(SELECT 1 FROM datasets WHERE display_id=:id)"),
            {"id": _dataset_display(name)},
        )
        outcome["stored_first_role"] = await observer.scalar(
            text("SELECT role FROM public.users WHERE email=:email"),
            {"email": specs["first"][0]},
        )
    _replay(name, outcome, record_property)


@pytest.mark.parametrize(
    "scenario", ["new", "existing", "missing_owner", "partial", "marker", "modified"]
)
async def test_large_image_owner_behavior(
    media_world, scenario, monkeypatch, record_property
):
    owner, producer = media_world
    name = "large-" + scenario
    email = name + "@seed.test"
    actor_id = None
    if scenario != "missing_owner":
        actor_id, _ = await _actor(owner, email, role="viewer", active=False)
    monkeypatch.setattr(seed_large_images, "async_session", producer)
    monkeypatch.setattr(seed_large_images, "PROJECT_DISPLAY_ID", "P-" + name)
    monkeypatch.setattr(seed_large_images, "DATASET_DISPLAY_ID", _dataset_display(name))
    if scenario not in {"new", "missing_owner"}:
        async with owner.begin() as db:
            project = Project(
                display_id="P-" + name,
                name="changed"
                if scenario == "modified"
                else seed_large_images.PROJECT_NAME,
                type_label="图像 · 实例分割",
                type_key="image-seg",
                data_type="image",
                owner_id=actor_id,
                ai_enabled=False,
                ai_interactive_enabled=False,
                tool_bindings=seed_large_images.TOOL_BINDINGS,
            )
            db.add(project)
            if scenario != "partial":
                dataset = Dataset(
                    display_id=_dataset_display(name),
                    name=seed_large_images.DATASET_NAME,
                    description="NASA 真实超大图开发、浏览器回归与文档截图夹具",
                    data_type="image",
                    created_by=actor_id,
                    metadata_={}
                    if scenario == "marker"
                    else seed_large_images._dataset_marker(1),
                )
                db.add(dataset)
                await db.flush()
                db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    try:
        dataset, project = await seed_large_images._ensure_seed_graph(
            owner_email=email, manifest_version=1
        )
        outcome = {
            "owner_reused": project.owner_id == actor_id
            and dataset.created_by == actor_id
        }
    except seed_large_images.LargeImageSeedError as exc:
        outcome = {"error": type(exc).__name__, "message": str(exc)}
    async with owner() as observer:
        project = (
            (
                await observer.execute(
                    text(
                        "SELECT name,type_label,type_key,data_type,ai_enabled,ai_interactive_enabled,scene_mode,prefer_same_scene_continuation FROM public.projects WHERE display_id=:id"
                    ),
                    {"id": "P-" + name},
                )
            )
            .mappings()
            .one_or_none()
        )
        dataset = (
            (
                await observer.execute(
                    text(
                        "SELECT name,description,data_type,metadata FROM public.datasets WHERE display_id=:id"
                    ),
                    {"id": _dataset_display(name)},
                )
            )
            .mappings()
            .one_or_none()
        )
        outcome["project"] = dict(project) if project else None
        outcome["dataset"] = dict(dataset) if dataset else None
        outcome["links"] = await observer.scalar(
            text(
                "SELECT count(*) FROM project_datasets AS link JOIN projects ON projects.id=link.project_id WHERE projects.display_id=:id"
            ),
            {"id": "P-" + name},
        )
    _replay(name, outcome, record_property)
