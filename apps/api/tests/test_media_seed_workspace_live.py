"""Cold media seed acceptance through the installed, Workspace-fenced API image."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import (
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_MEDIA_SEED_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace media-seed verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "apps/api/tests/fixtures/media_seed_workspace_probe.py"
_MEDIA_SEED_FUNCTIONS = (
    "aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)",
    "aap_workspace_v1.write_seed_project_projection(uuid,jsonb)",
    "aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)",
)
_IDENTITY_OPERATOR_FUNCTIONS = (
    "aap_identity_v1.read_provisioning_account(text)",
    "aap_identity_v1.create_seed_persona_account(jsonb)",
)
_RESULT_PREFIX = "AAP_MEDIA_SEED_WORKSPACE_RESULT="


def _tagged_ids(tag: str) -> tuple[list[str], list[str]]:
    suffix = tag.upper()
    result = (
        [
            f"P-MSC-{suffix}-COCO8",
            f"P-MSC-{suffix}-VIDEO",
            f"P-MSC-{suffix}-OCR",
            f"P-MSC-{suffix}-LARGE",
        ],
        [
            f"D-MSC-{suffix}-COCO8",
            f"D-MSC-{suffix}-VIDEO",
            f"D-MSC-{suffix}-OCR",
            f"D-MSC-{suffix}-LARGE",
        ],
    )
    assert all(len(value) <= 20 for group in result for value in group)
    return result


def _probe_result(run) -> dict:
    matches = [
        line[len(_RESULT_PREFIX) :]
        for line in run.stdout.splitlines()
        if line.startswith(_RESULT_PREFIX)
    ]
    assert len(matches) == 1, run.evidence
    return json.loads(matches[0])


def _create_media_storage_scope(resources, owner_s3, bucket: str, settings, tag: str):
    """Create an S3 principal that can only upload under this test's media prefix."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner_s3)
    )
    scope["bucket"] = bucket
    scope["policy"] = f"aap-media-seed-{secrets.token_hex(8)}"
    object_arn = f"arn:aws:s3:::{bucket}/media-seed/{tag}/*"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [object_arn],
            }
        ],
    }
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope, _destroy_cleanup_storage_scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


async def _assert_seed_profile(test_engine) -> None:
    _verify_postgres(test_engine.url)
    runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime_url.drivername == "postgresql+asyncpg"
    assert runtime_url.username == "legacy_runtime"
    assert (runtime_url.host, runtime_url.port, runtime_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert str(runtime_url.database).endswith("_test")
    runtime = create_async_engine(runtime_url, hide_parameters=True)
    try:
        async with runtime.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            assert await db.scalar(text("SELECT session_user")) == "legacy_runtime"
            role_flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls "
                        "FROM pg_catalog.pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert role_flags == (False, False, False, False, False)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname=current_user)"
                    )
                )
                == 0
            )
            for signature in (*_MEDIA_SEED_FUNCTIONS, *_IDENTITY_OPERATOR_FUNCTIONS):
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ),
                    {"signature": signature},
                ), signature
            for relation in ("public.projects", "public.project_members"):
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, :relation, "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"relation": relation},
                ), relation
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user, :relation, "
                        "'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"relation": relation},
                ), relation
            for relation in (
                "aap_workspace_v1.project_facts",
                "aap_workspace_v1.project_member_facts",
            ):
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, :relation, 'SELECT')"
                    ),
                    {"relation": relation},
                ), relation
    finally:
        await runtime.dispose()

    async with test_engine.connect() as db:
        for signature in _MEDIA_SEED_FUNCTIONS:
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege('identity_api_consumer', "
                    "CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
        for relation in ("public.projects", "public.project_members"):
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                    ":relation, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"relation": relation},
            ), relation


def _plain(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


async def _media_snapshot(test_engine, tag: str) -> dict:
    project_ids, dataset_ids = _tagged_ids(tag)
    async with test_engine.connect() as db:
        projects = (
            (
                await db.execute(
                    text(
                        "SELECT id,display_id,owner_id,type_key,data_type,tool_bindings "
                        "FROM public.projects WHERE display_id=ANY(CAST(:ids AS text[])) "
                        "ORDER BY display_id"
                    ),
                    {"ids": project_ids},
                )
            )
            .mappings()
            .all()
        )
        datasets = (
            (
                await db.execute(
                    text(
                        "SELECT id,display_id,name,data_type,file_count,created_by,metadata "
                        "FROM public.datasets WHERE display_id=ANY(CAST(:ids AS text[])) "
                        "ORDER BY display_id"
                    ),
                    {"ids": dataset_ids},
                )
            )
            .mappings()
            .all()
        )
        project_uuid = [row["id"] for row in projects]
        dataset_uuid = [row["id"] for row in datasets]
        links = (
            (
                await db.execute(
                    text(
                        "SELECT id,project_id,dataset_id FROM public.project_datasets "
                        "WHERE project_id=ANY(CAST(:project_ids AS uuid[])) "
                        "OR dataset_id=ANY(CAST(:dataset_ids AS uuid[])) ORDER BY id"
                    ),
                    {"project_ids": project_uuid, "dataset_ids": dataset_uuid},
                )
            )
            .mappings()
            .all()
        )
        items = (
            (
                await db.execute(
                    text(
                        "SELECT id,dataset_id,file_name,file_path,file_type,file_size,"
                        "content_hash,width,height,metadata FROM public.dataset_items "
                        "WHERE dataset_id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
                    ),
                    {"ids": dataset_uuid},
                )
            )
            .mappings()
            .all()
        )
        tasks = (
            (
                await db.execute(
                    text(
                        "SELECT id,project_id,display_id FROM public.tasks "
                        "WHERE project_id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
                    ),
                    {"ids": project_uuid},
                )
            )
            .mappings()
            .all()
        )
        predictions = (
            (
                await db.execute(
                    text(
                        "SELECT project_id,count(*) AS count FROM public.predictions "
                        "WHERE project_id=ANY(CAST(:ids AS uuid[])) GROUP BY project_id "
                        "ORDER BY project_id"
                    ),
                    {"ids": project_uuid},
                )
            )
            .mappings()
            .all()
        )
        frames = (
            (
                await db.execute(
                    text(
                        "SELECT item.dataset_id,count(*) AS count "
                        "FROM public.video_frame_indices AS frame "
                        "JOIN public.dataset_items AS item ON item.id=frame.dataset_item_id "
                        "WHERE item.dataset_id=ANY(CAST(:ids AS uuid[])) "
                        "GROUP BY item.dataset_id ORDER BY item.dataset_id"
                    ),
                    {"ids": dataset_uuid},
                )
            )
            .mappings()
            .all()
        )
    return {
        "projects": [_plain(dict(row)) for row in projects],
        "datasets": [_plain(dict(row)) for row in datasets],
        "links": [_plain(dict(row)) for row in links],
        "items": [_plain(dict(row)) for row in items],
        "tasks": [_plain(dict(row)) for row in tasks],
        "predictions": {str(row["project_id"]): row["count"] for row in predictions},
        "frames": {str(row["dataset_id"]): row["count"] for row in frames},
    }


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object_snapshot(owner_s3, bucket: str, prefix: str) -> dict[str, str]:
    result = {}
    for page in owner_s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix.rstrip("/") + "/"
    ):
        for item in page.get("Contents", []):
            response = owner_s3.get_object(Bucket=bucket, Key=item["Key"])
            result[item["Key"]] = hashlib.sha256(response["Body"].read()).hexdigest()
    return result


def _assert_media_state(state: dict, tag: str, owner_id: str) -> None:
    expected_projects, expected_datasets = _tagged_ids(tag)
    assert [row["display_id"] for row in state["projects"]] == sorted(expected_projects)
    assert [row["display_id"] for row in state["datasets"]] == sorted(expected_datasets)
    assert len(state["links"]) == 4
    assert len(state["items"]) == 4

    projects = {row["display_id"]: row for row in state["projects"]}
    datasets = {row["display_id"]: row for row in state["datasets"]}
    project_id_by_name = {name: row["id"] for name, row in projects.items()}
    dataset_id_by_name = {name: row["id"] for name, row in datasets.items()}
    assert all(row["owner_id"] == owner_id for row in state["projects"])
    assert all(row["created_by"] == owner_id for row in state["datasets"])
    assert {row["type_key"] for row in state["projects"]} == {
        "image-det",
        "video-track",
        "image-ocr",
        "image-seg",
    }
    assert {row["data_type"] for row in state["datasets"]} == {"image", "video"}

    item_counts = {dataset_id: 0 for dataset_id in dataset_id_by_name.values()}
    task_counts = {project_id: 0 for project_id in project_id_by_name.values()}
    for row in state["items"]:
        item_counts[row["dataset_id"]] += 1
    for row in state["tasks"]:
        task_counts[row["project_id"]] += 1
    assert item_counts[dataset_id_by_name[f"D-MSC-{tag.upper()}-COCO8"]] == 2
    assert item_counts[dataset_id_by_name[f"D-MSC-{tag.upper()}-VIDEO"]] == 1
    assert item_counts[dataset_id_by_name[f"D-MSC-{tag.upper()}-OCR"]] == 1
    assert item_counts[dataset_id_by_name[f"D-MSC-{tag.upper()}-LARGE"]] == 0
    assert task_counts[project_id_by_name[f"P-MSC-{tag.upper()}-COCO8"]] == 2
    assert task_counts[project_id_by_name[f"P-MSC-{tag.upper()}-VIDEO"]] == 1
    assert task_counts[project_id_by_name[f"P-MSC-{tag.upper()}-OCR"]] == 1
    assert task_counts[project_id_by_name[f"P-MSC-{tag.upper()}-LARGE"]] == 0
    assert (
        state["predictions"].get(project_id_by_name[f"P-MSC-{tag.upper()}-COCO8"], 0)
        == 2
    )
    assert state["frames"].get(dataset_id_by_name[f"D-MSC-{tag.upper()}-VIDEO"], 0) > 0
    assert all(row["file_count"] == item_counts[row["id"]] for row in state["datasets"])


def _without_project_bindings(state: dict) -> dict:
    result = dict(state)
    result["projects"] = [
        {key: value for key, value in row.items() if key != "tool_bindings"}
        for row in state["projects"]
    ]
    return result


async def test_media_seed_consumers_run_under_workspace_native_fences(
    test_engine, tmp_path, record_property
):
    from app.config import settings
    from PIL import Image

    _verify_postgres(test_engine.url)
    await _assert_seed_profile(test_engine)
    # Only the Workspace screenshot operations are qualified here; the media
    # bytes and full recording workflow remain outside this fixture.
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from tests.test_screenshot_seed_catalog import _ready_profile
    from scripts.seed_screenshot_profile import FIXED_TIME

    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        screenshot_projects, _screenshot_tasks = await _ready_profile(db)
        screenshot_ids = [project.id for project in screenshot_projects.values()]
        screenshot_image_id = screenshot_projects["image_demo"].id
    resources, _main_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    datasets_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    _verify_bucket_owner(owner_s3, datasets_bucket, resources["owner"])

    tag = uuid.uuid4().hex[:8]
    project_ids, dataset_ids = _tagged_ids(tag)
    async with test_engine.connect() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.projects "
                    "WHERE display_id=ANY(CAST(:ids AS text[]))"
                ),
                {"ids": project_ids},
            )
            == 0
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.datasets "
                    "WHERE display_id=ANY(CAST(:ids AS text[]))"
                ),
                {"ids": dataset_ids},
            )
            == 0
        )

    prefix = f"media-seed/{tag}"
    assert _object_snapshot(owner_s3, datasets_bucket, prefix) == {}
    source_root = tmp_path / f"workspace-media-seed-{tag}"
    coco8 = source_root / "coco8"
    for split, image_name in (("train", "a"), ("val", "b")):
        image_dir = coco8 / "images" / split
        label_dir = coco8 / "labels" / split
        image_dir.mkdir(parents=True)
        label_dir.mkdir(parents=True)
        Image.new("RGB", (8, 6), color=(30, 90, 140)).save(
            image_dir / f"{image_name}.jpg"
        )
        (label_dir / f"{image_name}.txt").write_text(
            "0 0.5 0.5 0.5 0.5\n", encoding="utf-8"
        )

    video_source = ROOT / "docs-site/public/home/sam-tools/smart-point.webm"
    assert video_source.is_file()
    video_fixture = source_root / "video" / video_source.name
    video_fixture.parent.mkdir(parents=True)
    shutil.copyfile(video_source, video_fixture)
    ocr_fixture = source_root / "ocr" / "ocr.jpg"
    ocr_fixture.parent.mkdir(parents=True)
    Image.new("RGB", (8, 6), color=(220, 220, 220)).save(ocr_fixture)
    shutil.copyfile(PROBE, source_root / PROBE.name)

    endpoint = urlsplit(endpoint_url())
    assert (
        endpoint.scheme in {"http", "https"}
        and endpoint.hostname
        and endpoint.username is None
        and endpoint.password is None
        and endpoint.path in {"", "/"}
        and not endpoint.query
        and not endpoint.fragment
    )
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    scope, destroy_scope = _create_media_storage_scope(
        resources, owner_s3, datasets_bucket, settings, tag
    )
    cli = None
    try:
        assert scope["access_key"] != settings.minio_access_key
        assert scope["secret_key"] != settings.minio_secret_key
        cli = WorkspaceImportCli(
            test_engine,
            tmp_path,
            source_root=source_root,
            buckets=resources["buckets"],
            minio_authority=f"{authority_host}:{endpoint.port or 9000}",
            minio_use_ssl=endpoint.scheme == "https",
            s3_access_key=scope["access_key"],
            s3_secret_key=scope["secret_key"],
        )
        await asyncio.to_thread(cli.start)
        await _assert_seed_profile(test_engine)

        async def run_probe(operation: str):
            return await asyncio.to_thread(
                cli.run_command,
                ("python", f"/fixture/{PROBE.name}", operation, tag),
            )

        create_signature = _MEDIA_SEED_FUNCTIONS[0]
        async with test_engine.begin() as owner:
            assert await owner.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege('legacy_runtime', "
                    "CAST(:signature AS regprocedure), 'EXECUTE')"
                ),
                {"signature": create_signature},
            )
            await owner.execute(
                text(
                    f"REVOKE EXECUTE ON FUNCTION {create_signature} FROM legacy_runtime"
                )
            )
        try:
            missing = await run_probe("missing_create")
            record_property("workspace_media_seed_missing_create", missing.evidence)
            assert not missing.timed_out
            assert missing.exit_code != 0, missing.evidence
            missing_result = _probe_result(missing)
            assert missing_result["operation"] == "missing_create"
            assert "permission denied for function create_media_seed_project" in (
                missing_result["error"].lower()
            ), missing.evidence
            assert await _media_snapshot(test_engine, tag) == {
                "projects": [],
                "datasets": [],
                "links": [],
                "items": [],
                "tasks": [],
                "predictions": {},
                "frames": {},
            }
            assert _object_snapshot(owner_s3, datasets_bucket, prefix) == {}
        finally:
            async with test_engine.begin() as owner:
                await owner.execute(
                    text(
                        f"GRANT EXECUTE ON FUNCTION {create_signature} TO legacy_runtime"
                    )
                )

        seeded = await run_probe("seed")
        record_property("workspace_media_seed_initial", seeded.evidence)
        assert not seeded.timed_out and seeded.exit_code == 0, seeded.evidence
        seeded_result = _probe_result(seeded)
        assert seeded_result["operation"] == "seed"
        owner_id = seeded_result["owner_id"]
        initial_state = await _media_snapshot(test_engine, tag)
        _assert_media_state(initial_state, tag, owner_id)
        large = seeded_result["large_graph"]
        large_project = next(
            row
            for row in initial_state["projects"]
            if row["display_id"] == f"P-MSC-{tag.upper()}-LARGE"
        )
        large_dataset = next(
            row
            for row in initial_state["datasets"]
            if row["display_id"] == f"D-MSC-{tag.upper()}-LARGE"
        )
        assert large_project["id"] == large["project_id"]
        assert large_dataset["id"] == large["dataset_id"]

        expected_objects = {
            f"{prefix}/coco8/train/a.jpg": _source_hash(coco8 / "images/train/a.jpg"),
            f"{prefix}/coco8/val/b.jpg": _source_hash(coco8 / "images/val/b.jpg"),
            f"{prefix}/video/{video_fixture.name}": _source_hash(video_fixture),
            f"{prefix}/ocr/{ocr_fixture.name}": _source_hash(ocr_fixture),
        }
        initial_objects = _object_snapshot(owner_s3, datasets_bucket, prefix)
        assert initial_objects == expected_objects
        expected_item_hashes = {
            f"{prefix}/coco8/train/a.jpg": expected_objects[
                f"{prefix}/coco8/train/a.jpg"
            ],
            f"{prefix}/coco8/val/b.jpg": expected_objects[f"{prefix}/coco8/val/b.jpg"],
            f"{prefix}/video/{video_fixture.name}": expected_objects[
                f"{prefix}/video/{video_fixture.name}"
            ],
            f"{prefix}/ocr/{ocr_fixture.name}": expected_objects[
                f"{prefix}/ocr/{ocr_fixture.name}"
            ],
        }
        assert {
            row["file_path"]: row["content_hash"] for row in initial_state["items"]
        } == (expected_item_hashes)

        replay = await run_probe("replay")
        record_property("workspace_media_seed_replay", replay.evidence)
        assert not replay.timed_out and replay.exit_code == 0, replay.evidence
        replay_result = _probe_result(replay)
        assert replay_result["operation"] == "replay"
        assert replay_result["large_graph"]["project_id"] == large["project_id"]
        assert replay_result["large_graph"]["dataset_id"] == large["dataset_id"]
        assert replay_result["results"]["coco8"] is None
        assert replay_result["results"]["video"] is None
        assert replay_result["results"]["ocr"] == {
            "project": f"P-MSC-{tag.upper()}-OCR",
            "skipped": True,
        }
        assert await _media_snapshot(test_engine, tag) == initial_state
        assert _object_snapshot(owner_s3, datasets_bucket, prefix) == initial_objects

        coco_display_id = f"P-MSC-{tag.upper()}-COCO8"
        custom_bindings = {
            "bbox": {
                "enabled": False,
                "classes": [{"name": "operator box", "order": 17}],
            },
            "region": {
                "enabled": False,
                "classes": [{"name": "operator region", "order": 9}],
            },
            "custom": {"preserve": True},
        }
        async with test_engine.begin() as owner:
            update = await owner.execute(
                text(
                    "UPDATE public.projects SET tool_bindings=CAST(:bindings AS jsonb) "
                    "WHERE display_id=:display_id"
                ),
                {
                    "bindings": json.dumps(custom_bindings),
                    "display_id": coco_display_id,
                },
            )
            assert update.rowcount == 1
        merge = await run_probe("coco_merge")
        record_property("workspace_media_seed_coco_binding_merge", merge.evidence)
        assert not merge.timed_out and merge.exit_code == 0, merge.evidence
        merge_result = _probe_result(merge)
        assert merge_result["first"] == {
            "project": coco_display_id,
            "added_tool_units": ["rotated_bbox", "polyline"],
        }
        assert merge_result["second"] is None

        final_state = await _media_snapshot(test_engine, tag)
        assert _without_project_bindings(final_state) == _without_project_bindings(
            initial_state
        )
        merged_project = next(
            row
            for row in final_state["projects"]
            if row["display_id"] == coco_display_id
        )
        bindings = merged_project["tool_bindings"]
        assert bindings["bbox"] == custom_bindings["bbox"]
        assert bindings["region"] == custom_bindings["region"]
        assert bindings["custom"] == custom_bindings["custom"]
        assert bindings["rotated_bbox"] == {
            "enabled": True,
            "classes": [
                {"name": "car", "order": 0},
                {"name": "bus", "order": 1},
                {"name": "truck", "order": 2},
            ],
        }
        assert bindings["polyline"] == {
            "enabled": True,
            "classes": [
                {"name": "lane marking", "order": 0},
                {"name": "curb", "order": 1},
            ],
        }
        assert _object_snapshot(owner_s3, datasets_bucket, prefix) == initial_objects
        for attempt in range(2):
            screenshot = await run_probe("screenshot")
            record_property(f"workspace_screenshot_seed_{attempt}", screenshot.evidence)
            assert not screenshot.timed_out and screenshot.exit_code == 0, (
                screenshot.evidence
            )
            result = _probe_result(screenshot)
            assert result["report"] == {"projects": 5, "tasks": 50, "batches": 5}
            assert result["backend"] == {"mode": "live", "bindings": {}}
            async with test_engine.connect() as db:
                rows = (
                    await db.execute(
                        text(
                            "SELECT created_at,updated_at,ml_backend_pool_id,preannotate_pipeline FROM projects WHERE id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": screenshot_ids},
                    )
                ).all()
                assert len(rows) == 5
                assert all(
                    tuple(row) == (FIXED_TIME, FIXED_TIME, None, []) for row in rows
                )
                assert (
                    await db.scalars(
                        text(
                            "SELECT assigned_at FROM project_members WHERE project_id=:id"
                        ),
                        {"id": screenshot_image_id},
                    )
                ).all() == [FIXED_TIME, FIXED_TIME]
        assert _object_snapshot(owner_s3, datasets_bucket, prefix) == initial_objects
        record_property(
            "workspace_media_seed_final_state",
            json.dumps(
                {"database": final_state, "objects": initial_objects},
                default=str,
                sort_keys=True,
            ),
        )
    finally:
        try:
            if cli is not None:
                await asyncio.to_thread(cli.close)
        finally:
            destroy_scope(scope)
