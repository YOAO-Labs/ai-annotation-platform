"""Replay media seed Identity behavior against owner-denied consumer credentials."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.core.security import hash_password, verify_password
from app.db.models.dataset import Dataset, ProjectDataset
from app.db.models.fixture_run import FixtureRunResource
from app.db.models.project import Project
from app.db.models.user import User
from app.services import screenshot_seed_catalog as catalog
from app.services.identity_commands import IdentityCommandService
from app.services.screenshot_seed_spec import USER_SPECS, fixture_persona_email
from scripts import (
    seed_coco8,
    seed_large_images,
    seed_ocr,
    seed_screenshot_profile,
    seed_video,
)
from tests.test_workspace_consumer_profile_native import _grants, _world

pytest_plugins = ("tests.screenshot_seed_support",)

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
    # Preserve frozen behavior, excluding SQL-count metadata and wording.
    expected = {
        key: item
        for key, item in GOLDEN[name].items()
        if key not in {"reads", "message"}
    }
    assert value == expected
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
    preferences = fields.pop("preferences", None)
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
        if preferences is not None:
            await db.execute(
                text(
                    "UPDATE public.identity_account_preferences "
                    "SET preferences=CAST(:preferences AS jsonb) WHERE user_id=:id"
                ),
                {"id": user.id, "preferences": json.dumps(preferences)},
            )
        return user.id, user.password_hash


async def _full_account(owner, email, *, original_hash=None):
    async with owner() as db:
        raw = await db.scalar(
            text(
                "SELECT to_jsonb(account) || jsonb_build_object("
                "'status', presence.status, 'last_seen_at', presence.last_seen_at, "
                "'preferences', prefs.preferences) "
                "FROM public.users AS account "
                "JOIN public.identity_account_presence AS presence ON presence.user_id=account.id "
                "JOIN public.identity_account_preferences AS prefs ON prefs.user_id=account.id "
                "WHERE account.email=:email"
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


@pytest.mark.parametrize("scenario", ["valid", "missing", "wrong_role", "inactive"])
async def test_screenshot_personas_use_exact_owned_accounts(screenshot_seed, scenario):
    run_id = screenshot_seed.run_id
    async with screenshot_seed.sessions() as db:
        user_id = await db.scalar(
            select(FixtureRunResource.resource_id).where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "user",
                FixtureRunResource.logical_key == "annotator",
                FixtureRunResource.state == "published",
            )
        )
        user = await db.get(User, user_id)
        assert user is not None
        email = fixture_persona_email(run_id, USER_SPECS["annotator"][0])
        if scenario == "missing":
            user.email = f"absent-{run_id.hex}"
        elif scenario == "wrong_role":
            user.role = "viewer"
        elif scenario == "inactive":
            user.is_active = False
        await db.flush()
        commits = _commits(db)
        if scenario == "valid":
            users = await seed_screenshot_profile._seed_users(db, run_id)
            assert users["annotator"] == user_id
            assert set(users) == set(USER_SPECS)
        else:
            with pytest.raises(seed_screenshot_profile.ScreenshotSeedReconcileError):
                await seed_screenshot_profile._seed_users(db, run_id)
        assert commits[0] == 0

        if scenario == "valid":
            result = await catalog.build_screenshot_seed_catalog(db, run_id=run_id)
            assert result["users"]["annotator"]["id"] == str(user_id)
            assert result["users"]["annotator"]["email"] == email
        else:
            with pytest.raises(catalog.ScreenshotSeedCatalogError):
                await catalog.build_screenshot_seed_catalog(db, run_id=run_id)
        await db.rollback()


@pytest.mark.parametrize("scenario", ["valid", "missing", "wrong_role", "inactive"])
async def test_restricted_identity_reads_run_owned_personas(
    media_world, screenshot_seed, scenario
):
    _, producer = media_world
    run_id = screenshot_seed.run_id
    async with screenshot_seed.sessions() as owner:
        user_id = await owner.scalar(
            select(FixtureRunResource.resource_id).where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "user",
                FixtureRunResource.logical_key == "annotator",
                FixtureRunResource.state == "published",
            )
        )
        user = await owner.get(User, user_id)
        assert user is not None
        original = (user.email, user.role, user.is_active)

    failure = None
    try:
        async with screenshot_seed.sessions.begin() as owner:
            user = await owner.get(User, user_id)
            if scenario == "missing":
                user.email = f"absent-{run_id.hex}"
            elif scenario == "wrong_role":
                user.role = "viewer"
            elif scenario == "inactive":
                user.is_active = False

        async with producer() as db:
            for query in (
                "SELECT id FROM public.users LIMIT 1",
                "SELECT resource_id FROM yoao_fixture_v1.fixture_run_resources LIMIT 1",
            ):
                with pytest.raises(DBAPIError) as denied:
                    await db.execute(text(query))
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await db.rollback()

            commits = _commits(db)
            rows = await IdentityCommandService(db).lookup_seed_account_facts(
                original[0]
            )
            if scenario == "missing":
                assert rows == []
            else:
                assert rows == [
                    (
                        user_id,
                        original[0],
                        "viewer" if scenario == "wrong_role" else original[1],
                        False if scenario == "inactive" else original[2],
                    )
                ]
            assert commits[0] == 0
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            async with screenshot_seed.sessions.begin() as owner:
                user = await owner.get(User, user_id)
                user.email, user.role, user.is_active = original
        except BaseException as restore_failure:
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "Restricted Identity read and persona restoration failed",
                    [failure, restore_failure],
                ) from None
            raise


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
        outcome = {"error": type(exc).__name__}
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
