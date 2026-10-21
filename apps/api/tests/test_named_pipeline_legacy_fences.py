"""Capture actual pre-extraction apply locks and atomic rollback once."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = "252ba82140f943c7b7dc8ab97d61be6ce0708eaa"
FILES = (
    "apps/api/app/api/v1/project_pipelines.py",
    "apps/api/app/api/v1/projects.py",
    "apps/api/app/application/project_pipeline.py",
    "apps/api/app/services/pipeline_template.py",
    "apps/api/app/services/project_access.py",
    "apps/inference/src/aap_inference/pipeline.py",
)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_CAPTURE_NAMED_PIPELINE_LEGACY_FENCES") != "1",
        reason="requires explicit pre-extraction capture on owned resources",
    ),
]


def _legacy_sources():
    hashes = {}
    for relative in FILES:
        frozen = subprocess.check_output(
            ["rtk", "proxy", "git", "show", f"{BASE}:{relative}"], cwd=ROOT
        )
        current = (ROOT / relative).read_bytes()
        assert current == frozen, f"Legacy source changed before capture: {relative}"
        hashes[relative] = hashlib.sha256(current).hexdigest()
    return hashes


def _normalise(value, aliases):
    if isinstance(value, dict):
        return {
            key: "$timestamp"
            if key in {"created_at", "updated_at"}
            else _normalise(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise(item, aliases) for item in value]
    return aliases.get(value, value) if isinstance(value, str) else value


async def _seed(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"pipeline-fence-{uuid.uuid4().hex}@example.test",
            "Pipeline owner",
        )
        project = await create_project(db, owner_id=actor.id)
        source = ProjectPipeline(
            id=uuid.uuid4(),
            scope="public",
            name="Frozen source",
            stages=[],
            is_default=False,
            created_by=actor.id,
        )
        default = ProjectPipeline(
            id=uuid.uuid4(),
            scope="private",
            project_id=project.id,
            name="Prior default",
            stages=[],
            is_default=True,
            created_by=actor.id,
        )
        db.add_all([source, default])
    return maker, actor, project.id, source.id, default.id


async def _cleanup(maker, actor_id, project_id, source_id):
    async with maker.begin() as db:
        await db.execute(
            delete(ProjectPipeline).where(ProjectPipeline.project_id == project_id)
        )
        await db.execute(delete(ProjectPipeline).where(ProjectPipeline.id == source_id))
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def _snapshot(maker, project_id, source_id, aliases):
    async with maker() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id::text, name, is_default, usage_count FROM public.project_pipelines "
                        "WHERE project_id=:project OR id=:source ORDER BY name,id"
                    ),
                    {"project": project_id, "source": source_id},
                )
            )
            .mappings()
            .all()
        )
        counter = await db.scalar(
            select(Project.total_tasks).where(Project.id == project_id)
        )
    return {
        "pipelines": _normalise([dict(row) for row in rows], aliases),
        "counter": counter,
    }


async def _update_lock_conflicts(engine, project_id):
    connection = await asyncpg.connect(
        engine.url.set(drivername="postgresql").render_as_string(hide_password=False),
        timeout=5,
        command_timeout=5,
    )
    try:
        async with connection.transaction():
            with pytest.raises(asyncpg.LockNotAvailableError) as error:
                await connection.fetchval(
                    "SELECT id FROM public.projects WHERE id=$1 FOR UPDATE NOWAIT",
                    project_id,
                )
            assert error.value.sqlstate == "55P03"
            return error.value.sqlstate
    finally:
        await connection.close()


async def _settle(task, release):
    release.set()
    if task is None:
        return
    done, _ = await asyncio.wait({task}, timeout=15)
    if not done:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


async def _capture(engine, app_module, monkeypatch, *, reject_insert):
    from app.application import project_pipeline as application

    maker, actor, project_id, source_id, default_id = await _seed(engine)
    aliases = {
        str(actor.id): "$actor",
        str(project_id): "$project",
        str(source_id): "$source",
        str(default_id): "$prior_default",
    }
    entered, release = asyncio.Event(), asyncio.Event()
    task = runtime = owner = None
    trigger = f"test_pipeline_fence_{uuid.uuid4().hex}"
    function = f"test_pipeline_fence_{uuid.uuid4().hex}"
    original = application.apply_pipeline

    async def paused(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        return await original(*args, **kwargs)

    try:
        if reject_insert:
            owner = await asyncpg.connect(
                engine.url.set(drivername="postgresql").render_as_string(
                    hide_password=False
                ),
                timeout=5,
                command_timeout=5,
            )
            await owner.execute(
                f"CREATE FUNCTION public.{function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.project_id = '{project_id}'::uuid AND NEW.name='Frozen source' "
                "THEN RAISE EXCEPTION 'owned named-pipeline insertion probe'; END IF; RETURN NEW; END $$"
            )
            await owner.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.project_pipelines "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
            )
        before = await _snapshot(maker, project_id, source_id, aliases)
        runtime, requests = await make_request_runtime(
            engine, application_name="named-pipeline-legacy-fences"
        )
        with monkeypatch.context() as scoped:
            clear_local_identity(scoped)
            if not reject_insert:
                scoped.setattr(application, "apply_pipeline", paused)
            async with api_db_binding(app_module, requests):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=app_module, raise_app_exceptions=False
                    ),
                    base_url="http://named-pipeline-legacy",
                    timeout=30,
                ) as client:
                    task = asyncio.create_task(
                        client.post(
                            f"/api/v1/projects/{project_id}/pipelines/apply",
                            headers={
                                "Authorization": f"Bearer {create_access_token(str(actor.id), actor.role)}"
                            },
                            json={"pipeline_id": str(source_id), "set_default": True},
                        )
                    )
                    observations = {}
                    if not reject_insert:
                        await asyncio.wait_for(entered.wait(), timeout=10)
                        observations[
                            "project_update_lock_sqlstate"
                        ] = await _update_lock_conflicts(engine, project_id)

                        async def counter_and_delete():
                            async with maker.begin() as db:
                                await db.execute(
                                    text(
                                        "UPDATE public.projects SET total_tasks=total_tasks+1 WHERE id=:id"
                                    ),
                                    {"id": project_id},
                                )
                                result = await db.execute(
                                    delete(ProjectPipeline).where(
                                        ProjectPipeline.id == source_id
                                    )
                                )
                                return result.rowcount

                        observations[
                            "source_deleted_while_fenced"
                        ] = await asyncio.wait_for(counter_and_delete(), timeout=5)
                        release.set()
                    response = await asyncio.wait_for(task, timeout=20)
                    if reject_insert:
                        assert response.status_code == 500
                        body = response.text
                    else:
                        assert response.status_code == 201, response.text
                        body = response.json()
                        aliases[body["id"]] = "$copy"
                        assert body["created_by_name"] is None
                        assert body["stages"] == [] and body["is_default"] is True
                    after = await _snapshot(maker, project_id, source_id, aliases)
                    if reject_insert:
                        assert after == before
                    else:
                        assert observations["source_deleted_while_fenced"] == 1
                        assert after["counter"] == before["counter"] + 1
                        assert all(row["id"] != "$source" for row in after["pipelines"])
                        assert len(after["pipelines"]) == 2
                        assert [
                            row["id"] for row in after["pipelines"] if row["is_default"]
                        ] == ["$copy"]
                    return {
                        "label": "atomic_insert_failure"
                        if reject_insert
                        else "key_share_and_deleted_source_snapshot",
                        "before": before,
                        "observations": observations,
                        "response": {
                            "status": response.status_code,
                            "content_type": response.headers["content-type"],
                            "body": _normalise(body, aliases),
                        },
                        "after": after,
                    }
    finally:
        await _settle(task, release)
        if runtime is not None:
            await runtime.dispose()
        try:
            if owner is not None:
                await owner.execute(
                    f"DROP TRIGGER IF EXISTS {trigger} ON public.project_pipelines"
                )
                await owner.execute(f"DROP FUNCTION IF EXISTS public.{function}()")
        finally:
            if owner is not None:
                await owner.close()
            await _cleanup(maker, actor.id, project_id, source_id)


async def test_capture_named_pipeline_legacy_apply_fences(
    test_engine, app_module, monkeypatch
):
    await assert_owned_test_database(test_engine)
    hashes = _legacy_sources()
    target = Path(__file__).parent / "fixtures/named_pipeline_legacy/locks.json"
    assert not target.exists(), "Do not overwrite the frozen legacy fence oracle"
    samples = []
    for reject in (False, True):
        samples.append(
            await _capture(test_engine, app_module, monkeypatch, reject_insert=reject)
        )
    target.parent.mkdir(exist_ok=True)
    document = {
        "schema": 1,
        "capture_kind": "actual-pre-extraction-named-pipeline-locks",
        "source_commit": BASE,
        "source_sha256": hashes,
        "samples": samples,
    }
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
