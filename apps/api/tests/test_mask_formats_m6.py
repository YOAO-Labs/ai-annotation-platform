from __future__ import annotations

import hashlib
import io
import json
import stat
import struct
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.mask_format import MaskFormatPlan
from app.services.exporting.cache import compute_cache_key
from app.services.mask_formats import registry
from app.services.mask_formats.adapters import _ALLOWED_GEOMETRY
from app.services.mask_formats.contracts import StagedObject
from app.services.mask_formats.safe_archive import (
    ArchiveLimits,
    ArchiveSafetyError,
    SafeZipArchive,
    validate_png_contract,
)
from app.services.storage import storage_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _zip_bytes(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def test_registry_is_versioned_and_verified_import_is_visible() -> None:
    format_ids = {
        adapter.descriptor.format_id
        for adapter in registry.list(media_type="image", direction="export")
    }
    assert format_ids >= {"aap_json", "coco", "yolo-seg", "voc"}
    aap = registry.get("aap_json").descriptor
    assert aap.adapter_version == "1.0.0"
    assert aap.manifest_version == "1.3"
    assert aap.import_capability.supported is True
    assert aap.import_capability.verified is True
    assert aap.import_capability.enabled_for_ui is True


def test_video_obb_and_keypoint_are_only_claimed_by_lossless_formats() -> None:
    assert _ALLOWED_GEOMETRY["aap_json"] is None
    assert _ALLOWED_GEOMETRY["video_json"] is None
    for format_id in (
        "mot",
        "yolo-frames-det",
        "yolo-frames-seg",
        "coco-frames-seg",
        "davis",
        "youtube-vos",
        "mots",
    ):
        allowed = _ALLOWED_GEOMETRY[format_id]
        assert allowed is not None
        assert "video_rotated_bbox" not in allowed
        assert "video_keypoint" not in allowed
    verified_imports = {
        adapter.descriptor.format_id
        for adapter in registry.list(
            media_type="image", direction="import", ui_only=True
        )
        if adapter.descriptor.import_capability.verified
    }
    assert verified_imports == {
        "aap_json",
        "binary-png",
        "coco",
        "indexed-png",
        "label-studio-brush",
        "yolo-seg",
    }


def test_export_cache_key_includes_adapter_and_options_contract() -> None:
    scope_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    base = compute_cache_key(
        scope_id,
        ["coco"],
        True,
        "keyframes",
        None,
        1,
        adapter_contracts={
            "coco": {"adapter_version": "1.0.0", "manifest_version": "1"}
        },
        options_digest="a" * 64,
    )
    assert base != compute_cache_key(
        scope_id,
        ["coco"],
        True,
        "keyframes",
        None,
        1,
        adapter_contracts={
            "coco": {"adapter_version": "1.0.1", "manifest_version": "1"}
        },
        options_digest="a" * 64,
    )
    assert base != compute_cache_key(
        scope_id,
        ["coco"],
        True,
        "keyframes",
        None,
        1,
        adapter_contracts={
            "coco": {"adapter_version": "1.0.0", "manifest_version": "1"}
        },
        options_digest="b" * 64,
    )


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ([("../escape.txt", b"x")], "archive_path_traversal"),
        ([("/absolute.txt", b"x")], "archive_path_absolute"),
        ([("A/mask.png", b"x"), ("a/mask.png", b"y")], "archive_casefold_collision"),
        ([("same.txt", b"x"), ("same.txt", b"y")], "archive_duplicate_path"),
    ],
)
def test_safe_archive_rejects_unsafe_paths(entries, reason) -> None:
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(io.BytesIO(_zip_bytes(entries)), ArchiveLimits())
    assert caught.value.code == reason


def test_safe_archive_rejects_symlink_and_zip_bomb() -> None:
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(_zip_bytes([(symlink, b"target")])),
            ArchiveLimits(),
        )
    assert caught.value.code == "archive_symlink"

    bomb = _zip_bytes([("zeros.bin", b"\0" * 1024 * 1024)])
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(bomb),
            ArchiveLimits(max_compression_ratio=10),
        )
    assert caught.value.code == "archive_compression_ratio_exceeded"


def test_safe_archive_manifest_closure_and_png_contract() -> None:
    archive = SafeZipArchive(
        io.BytesIO(_zip_bytes([("masks/a.png", b"png")])),
        ArchiveLimits(),
    )
    try:
        with pytest.raises(ArchiveSafetyError) as caught:
            archive.require_paths(["masks/missing.png"])
        assert caught.value.code == "manifest_reference_missing"
    finally:
        archive.close()

    png_header = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", 5, 3, 8, 0, 0, 0, 0)
        + b"\0\0\0\0"
    )
    validate_png_contract(
        io.BytesIO(png_header),
        expected_width=5,
        expected_height=3,
    )
    with pytest.raises(ArchiveSafetyError) as caught:
        validate_png_contract(
            io.BytesIO(png_header),
            expected_width=6,
            expected_height=3,
        )
    assert caught.value.code == "image_size_mismatch"


async def _seed_aap_import(db, *, owner_id: uuid.UUID) -> tuple[Project, Task, bytes]:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        display_id=f"P-FMT-{suffix}",
        name=f"Format {suffix}",
        type_key="image-det",
        type_label="图像检测",
        data_type="image",
        owner_id=owner_id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-FMT-{suffix}",
        file_name="image.png",
        file_path="dataset/image.png",
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    payload = {
        "schema_version": "1.3",
        "tasks": [
            {
                "task_match": {"display_id": task.display_id},
                "file_path": task.file_path,
                "media_type": "image",
                "annotations": [
                    {
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.3,
                            "h": 0.2,
                        },
                        "class_name": "object",
                        "tool_unit_id": "bbox",
                        "source": "manual",
                    }
                ],
            }
        ],
    }
    return project, task, json.dumps(payload).encode()


@pytest.mark.asyncio
async def test_export_preflight_reports_loss_and_unsupported_items(
    httpx_client,
    super_admin,
    db_session,
) -> None:
    user, token = super_admin
    project, task, _payload = await _seed_aap_import(db_session, owner_id=user.id)
    db_session.add(
        Annotation(
            task_id=task.id,
            tool_unit_id="bbox",
            class_name="object",
            geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.2},
            attributes={},
            source="manual",
            is_active=True,
            was_cancelled=False,
        )
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/exports:preflight",
        headers=_bearer(token),
        json={"targets": ["aap_json", "yolo-seg"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["loss_class"] == "unsupported"
    assert [plan["format_id"] for plan in body["plans"]] == [
        "aap_json",
        "yolo-seg",
    ]
    yolo_plan = body["plans"][1]
    assert yolo_plan["items"][0]["loss_class"] == "unsupported"
    assert yolo_plan["items"][0]["warnings"][0]["code"] == "unsupported_geometry"
    assert len(body["preflight_digest"]) == 64


@pytest.mark.asyncio
async def test_staged_aap_preflight_receipt_dispatch_once_and_task_execution(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
    tmp_path,
) -> None:
    user, token = super_admin
    project, task, payload = await _seed_aap_import(db_session, owner_id=user.id)
    await db_session.commit()
    digest = hashlib.sha256(payload).hexdigest()
    object_key = f"mask-formats/{project.id}/{user.id}/{uuid.uuid4()}/annotations.json"

    class Body(io.BytesIO):
        pass

    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda *_args, **_kwargs: {"ContentLength": len(payload)},
    )
    monkeypatch.setattr(
        storage_service,
        "client",
        SimpleNamespace(get_object=lambda **_kwargs: {"Body": Body(payload)}),
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports:preflight",
        headers=_bearer(token),
        json={
            "format_id": "aap_json",
            "staged_object_key": object_key,
            "staged_sha256": digest,
            "mapping": {},
            "options": {"overwrite": False},
        },
    )
    assert response.status_code == 200, response.text
    preflight = response.json()
    assert preflight["plan"]["loss_class"] == "lossless"
    assert preflight["plan"]["items"][0]["task_id"] == str(task.id)

    dispatches: list[uuid.UUID] = []

    async def fake_dispatch(import_id: uuid.UUID) -> str:
        dispatches.append(import_id)
        return "celery-format-import-1"

    monkeypatch.setattr("app.api.v1.mask_formats.dispatch_import", fake_dispatch)
    execute_payload = {
        "receipt": preflight["receipt"],
        "plan_digest": preflight["plan"]["plan_digest"],
        "confirm_lossy": False,
    }
    first = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports",
        headers=_bearer(token),
        json=execute_payload,
    )
    replay = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports",
        headers=_bearer(token),
        json=execute_payload,
    )
    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["id"] == replay.json()["id"]
    assert len(dispatches) == 1

    local_file = tmp_path / "annotations.json"
    local_file.write_bytes(payload)
    adapter = registry.get("aap_json")
    result = await adapter.execute_import_item(
        db_session,
        project=project,
        staged=StagedObject(
            object_key=object_key,
            sha256=digest,
            local_path=str(local_file),
            size_bytes=len(payload),
        ),
        plan=MaskFormatPlan.model_validate(preflight["plan"]),
        item_index=0,
        operator_user_id=user.id,
        options={"overwrite": False},
    )
    await db_session.flush()
    assert result == {
        "status": "committed",
        "task_id": str(task.id),
        "imported": 1,
        "skipped": 0,
    }
    annotation = (
        await db_session.execute(
            select(Annotation).where(
                Annotation.task_id == task.id,
                Annotation.attributes["_imported"].astext == "true",
            )
        )
    ).scalar_one()
    assert annotation.class_name == "object"

    batch = await db_session.get(
        MaskFormatImport,
        uuid.UUID(first.json()["id"]),
    )
    assert batch is not None
    assert batch.async_job_id is not None

    revoked: list[str] = []
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.control.revoke",
        lambda task_id, **_kwargs: revoked.append(task_id),
    )
    cancel = await httpx_client.post(
        f"/api/v1/async-jobs/{batch.async_job_id}/cancel",
        headers=_bearer(token),
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    await db_session.refresh(batch)
    assert batch.status == "cancelled"
    assert revoked == ["celery-format-import-1"]


@pytest.mark.asyncio
async def test_import_execute_rejects_plan_without_executable_items(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
) -> None:
    user, token = super_admin
    project, _task, payload = await _seed_aap_import(db_session, owner_id=user.id)
    decoded = json.loads(payload)
    decoded["tasks"][0]["annotations"] = []
    payload = json.dumps(decoded).encode()
    await db_session.commit()
    digest = hashlib.sha256(payload).hexdigest()
    object_key = f"mask-formats/{project.id}/{user.id}/{uuid.uuid4()}/empty.json"

    class Body(io.BytesIO):
        pass

    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda *_args, **_kwargs: {"ContentLength": len(payload)},
    )
    monkeypatch.setattr(
        storage_service,
        "client",
        SimpleNamespace(get_object=lambda **_kwargs: {"Body": Body(payload)}),
    )
    preflight = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports:preflight",
        headers=_bearer(token),
        json={
            "format_id": "aap_json",
            "staged_object_key": object_key,
            "staged_sha256": digest,
            "mapping": {},
            "options": {},
        },
    )
    assert preflight.status_code == 200, preflight.text

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports",
        headers=_bearer(token),
        json={
            "receipt": preflight.json()["receipt"],
            "plan_digest": preflight.json()["plan"]["plan_digest"],
            "confirm_lossy": False,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "format_plan_has_no_executable_items"
