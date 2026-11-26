from __future__ import annotations

import base64
import hashlib
import json
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select

from yoao_labeling.format_adapter_contract import (
    EXTENSION_MANIFEST_SCHEMA,
    FORMAT_ADAPTER_PROTOCOL,
    FormatAdapterManifestV1,
    FormatAdapterSetupResponse,
    validate_format_id,
    validate_manifest_matches_setup,
    validate_option_schema,
    validate_safe_relative_path,
)
from yoao_labeling.format_adapter_security import (
    FormatAdapterHostDenied,
    FormatAdapterHttpClient,
    FormatAdapterKeyNotConfigured,
    decrypt_adapter_token,
    encrypt_adapter_token,
    validate_adapter_endpoint_url,
)
from yoao_labeling.format_adapter_store_postgres import (
    FormatAdapterStorePostgresData,
)

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.mask_formats.contracts import StagedObject
from app.services.mask_formats.dynamic_registry import resolve_format_adapter
from app.services.mask_formats.runtime import format_host


def test_format_adapter_manifest_and_contract_validation() -> None:
    manifest = FormatAdapterManifestV1.model_validate(
        {
            "manifest_schema": EXTENSION_MANIFEST_SCHEMA,
            "extension_point": "format_adapter",
            "plugin_id": "yoao.format.labelme-json",
            "version": "1.0.0",
            "display_name": "LabelMe JSON",
            "description": "Import and export LabelMe polygon and box JSON files.",
            "homepage": "https://github.com/yoao-ai/yoao-plugins",
            "publisher": "YOAO Official",
            "license": "MIT",
            "protocol_version": FORMAT_ADAPTER_PROTOCOL,
            "entrypoint": ["node", "src/server.mjs"],
            "capabilities": ["import", "export"],
            "resource": {"memory_mb": 256},
            "extension": {
                "format_id": "labelme-json",
                "adapter_version": "1.0.0",
                "manifest_version": "1",
                "media_types": ["image"],
                "option_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "default_label": {"type": "string", "maxLength": 64}
                    },
                },
                "allowed_egress_hosts": [],
            },
        }
    )
    bound = manifest.bind_artifact(f"ghcr.io/yoao/labelme@sha256:{'a' * 64}")
    assert bound.artifact is not None

    setup = FormatAdapterSetupResponse.model_validate(
        {
            "protocol_version": FORMAT_ADAPTER_PROTOCOL,
            "format_id": "labelme-json",
            "adapter_version": "1.0.0",
            "manifest_version": "1",
            "display_name": "LabelMe JSON",
            "media_types": ["image"],
            "capabilities": ["import", "export"],
            "option_schema": {},
        }
    )
    validate_manifest_matches_setup(bound, setup)

    with pytest.raises(ValueError, match="built-in"):
        validate_format_id("coco")
    with pytest.raises(ValueError, match="disallowed keys"):
        validate_option_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"x": {"type": "string", "pattern": "^(a+)+$"}},
            }
        )
    with pytest.raises(ValueError, match="unsafe export file path"):
        validate_safe_relative_path("../etc/passwd")
    with pytest.raises(ValidationError):
        FormatAdapterManifestV1.model_validate(
            {
                "manifest_schema": EXTENSION_MANIFEST_SCHEMA,
                "extension_point": "format_adapter",
                "plugin_id": "bad.shell",
                "version": "1.0.0",
                "publisher": "Test",
                "license": "MIT",
                "protocol_version": FORMAT_ADAPTER_PROTOCOL,
                "entrypoint": ["sh", "-c", "id"],
                "capabilities": ["import"],
                "resource": {"memory_mb": 128},
                "extension": {
                    "format_id": "bad-shell",
                    "adapter_version": "1.0.0",
                    "media_types": ["image"],
                },
            }
        )


def test_format_adapter_crypto_and_ssrf_guard() -> None:
    with pytest.raises(FormatAdapterKeyNotConfigured):
        encrypt_adapter_token("secret-token", "")

    key = Fernet.generate_key().decode("ascii")
    ct = encrypt_adapter_token("secret-token-123", key)
    assert decrypt_adapter_token(ct, key) == "secret-token-123"

    with pytest.raises(FormatAdapterHostDenied):
        validate_adapter_endpoint_url("http://127.0.0.1:9900")
    with pytest.raises(FormatAdapterHostDenied):
        validate_adapter_endpoint_url(
            "http://169.254.169.254/latest",
            host_allowlist=["169.254.169.254"],
            allow_insecure_http_hosts=["169.254.169.254"],
        )
    scheme, host, port, base_path, pinned_ip = validate_adapter_endpoint_url(
        "http://127.0.0.1:19123/base",
        host_allowlist=["127.0.0.1"],
        allow_insecure_http_hosts=["127.0.0.1"],
    )
    assert (scheme, host, port, base_path, pinned_ip) == (
        "http",
        "127.0.0.1",
        19123,
        "/base",
        "127.0.0.1",
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fake_plugin_transport(expected_token: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("Authorization") != f"Bearer {expected_token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.url.path == "/setup":
            return httpx.Response(
                200,
                json={
                    "protocol_version": FORMAT_ADAPTER_PROTOCOL,
                    "format_id": "labelme-ext-test",
                    "adapter_version": "1.0.0",
                    "manifest_version": "1",
                    "display_name": "LabelMe Ext Test",
                    "description": "Test external format adapter.",
                    "homepage": "https://example.com/labelme",
                    "media_types": ["image"],
                    "capabilities": ["import"],
                    "option_schema": {},
                },
            )
        if request.url.path == "/import/parse":
            body = json.loads(request.content.decode("utf-8"))
            raw_doc = json.loads(
                base64.b64decode(body["content_base64"]).decode("utf-8")
            )
            label_map = (body.get("mapping") or {}).get("labels") or {}
            items = []
            for entry in raw_doc["items"]:
                anns = []
                for shape in entry["shapes"]:
                    raw_label = shape["label"]
                    anns.append(
                        {
                            "class_name": label_map.get(raw_label, raw_label),
                            "tool_unit_id": "bbox",
                            "geometry": {
                                "type": "bbox",
                                "x": shape["bbox"][0],
                                "y": shape["bbox"][1],
                                "w": shape["bbox"][2],
                                "h": shape["bbox"][3],
                            },
                            "source_id": shape.get("id"),
                            "attributes": {},
                        }
                    )
                items.append(
                    {
                        "media_path": entry["imagePath"],
                        "width": entry["imageWidth"],
                        "height": entry["imageHeight"],
                        "losses": [],
                        "warnings": [],
                        "annotations": anns,
                        "mask_objects": {},
                    }
                )
            return httpx.Response(
                200,
                json={
                    "protocol_version": FORMAT_ADAPTER_PROTOCOL,
                    "items": items,
                },
            )
        return httpx.Response(404, json={"error": "not_found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_format_adapter_plugin_lifecycle_and_import_commit(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
    tmp_path,
) -> None:
    user, admin_token = super_admin
    fernet_key = Fernet.generate_key().decode("ascii")
    plugin_token = "ext-format-secret-token"
    monkeypatch.setattr(settings, "format_adapter_encryption_key", fernet_key)
    monkeypatch.setattr(settings, "format_adapter_host_allowlist", ["127.0.0.1"])
    monkeypatch.setattr(
        settings, "format_adapter_allow_insecure_http_hosts", ["127.0.0.1"]
    )
    mock_client = FormatAdapterHttpClient(
        host_allowlist=["127.0.0.1"],
        allow_insecure_http_hosts=["127.0.0.1"],
        transport=_fake_plugin_transport(plugin_token),
    )
    monkeypatch.setattr(
        "app.api.v1.format_adapter_plugins.build_format_adapter_http_client",
        lambda: mock_client,
    )
    monkeypatch.setattr(
        "app.services.mask_formats.dynamic_registry.build_format_adapter_http_client",
        lambda: mock_client,
    )

    reg = await httpx_client.post(
        "/api/v1/admin/format-adapters",
        headers=_bearer(admin_token),
        json={
            "endpoint_url": "http://127.0.0.1:19888",
            "auth_token": plugin_token,
            "mode": "direct",
            "source_tier": "third_party",
            "activate": True,
        },
    )
    assert reg.status_code == 201, reg.text
    created = reg.json()
    assert created["format_id"] == "labelme-ext-test"
    assert created["source_tier"] == "third_party"
    assert created["status"] == "active"
    assert created["revision"] == 1
    assert "auth_token_ciphertext" not in created

    probe = await httpx_client.post(
        "/api/v1/admin/format-adapters/labelme-ext-test/probe",
        headers=_bearer(admin_token),
    )
    assert probe.status_code == 200, probe.text
    assert probe.json()["healthy"] is True
    assert probe.json()["setup_ok"] is True

    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-FA-{suffix}",
        name=f"Format Adapter {suffix}",
        type_key="object-detection",
        type_label="目标检测",
        data_type="image",
        owner_id=user.id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    dataset = Dataset(
        display_id=f"DS-FA-{suffix}",
        name=f"Dataset {suffix}",
        data_type="image",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="sample.jpg",
        file_path=f"ds-{suffix}/sample.jpg",
        file_type="image",
        width=100,
        height=80,
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-FA-{suffix}",
        file_name="sample.jpg",
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    listed = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mask-formats",
        headers=_bearer(admin_token),
    )
    assert listed.status_code == 200, listed.text
    assert any(row["format_id"] == "labelme-ext-test" for row in listed.json())

    doc = {
        "items": [
            {
                "imagePath": "sample.jpg",
                "imageWidth": 100,
                "imageHeight": 80,
                "shapes": [
                    {"id": "s1", "label": "vehicle", "bbox": [0.1, 0.2, 0.3, 0.4]}
                ],
            }
        ]
    }
    raw_bytes = json.dumps(doc).encode("utf-8")
    staged_path = tmp_path / "labelme.json"
    staged_path.write_bytes(raw_bytes)
    staged = StagedObject(
        object_key="staged/labelme.json",
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        local_path=str(staged_path),
        size_bytes=len(raw_bytes),
    )

    adapter = await resolve_format_adapter(db_session, "labelme-ext-test")
    host, project_ctx = format_host(db_session, project)

    bad_plan = await adapter.preflight_import(
        host, project=project_ctx, staged=staged, mapping={}, options={}
    )
    assert bad_plan.loss_class == "unsupported"
    assert bad_plan.unknown_labels == ["vehicle"]

    mapping = {"labels": {"vehicle": "car"}}
    plan = await adapter.preflight_import(
        host, project=project_ctx, staged=staged, mapping=mapping, options={}
    )
    assert plan.loss_class == "lossless"
    assert plan.items[0].task_id == task.id

    commit_result = await adapter.execute_import_item(
        host,
        project=project_ctx,
        staged=staged,
        plan=plan,
        item_index=0,
        operator_user_id=user.id,
        mapping=mapping,
        options={},
    )
    assert commit_result == {
        "status": "committed",
        "task_id": str(task.id),
        "imported": 1,
        "skipped": 0,
    }
    ann = (
        await db_session.execute(
            select(Annotation).where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert ann.class_name == "car"
    assert ann.geometry["type"] == "bbox"
    assert ann.attributes["_import_format"] == "labelme-ext-test"

    disabled = await httpx_client.post(
        "/api/v1/admin/format-adapters/labelme-ext-test/disable",
        headers=_bearer(admin_token),
        json={"expected_revision": 1},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["revision"] == 2

    listed_after_disable = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mask-formats",
        headers=_bearer(admin_token),
    )
    assert not any(
        row["format_id"] == "labelme-ext-test" for row in listed_after_disable.json()
    )
    with pytest.raises(ValueError, match="unsupported format adapter"):
        await resolve_format_adapter(db_session, "labelme-ext-test")

    retired = await httpx_client.post(
        "/api/v1/admin/format-adapters/labelme-ext-test/retire",
        headers=_bearer(admin_token),
        json={"expected_revision": 2},
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "retired"

    secret_after_retire = await FormatAdapterStorePostgresData(db_session).open_secret(
        "labelme-ext-test"
    )
    assert secret_after_retire is not None
    assert secret_after_retire.auth_token_ciphertext is None
    still_exists = (
        await db_session.execute(
            select(Annotation).where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert still_exists.id == ann.id
