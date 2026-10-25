"""Mask upload ownership and saved-address reads; parsers keep their contracts."""

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import select

import app.services
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.mask_format_upload import MaskFormatUpload
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.managed_original_retirement import managed_storage_identity
from app.services.mask_formats import service
from tests.test_mask_formats_m6 import _bearer, _seed_aap_import


def external_ref(prefix):
    endpoint, bucket = "https://mask-saved.example.test", "mask-saved"
    return {
        "schema": 2,
        "purpose": "import",
        "location_id": str(uuid.UUID(int=711)),
        "config_revision": 3,
        "binding_generation": 2,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "key_prefix": "team/图片/_aap_managed/import/" + prefix,
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }


def physical_world(
    monkeypatch, payload=b"saved bytes", *, schema=2, head=None, body=None
):
    object_key = f"mask-formats/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}/a.json"
    prefix, relative = service.staged_parts(object_key)
    ref = external_ref(prefix)
    ref["schema"] = schema
    owner = SimpleNamespace(
        bucket=ref["bucket"],
        ref=ref,
        key=lambda suffix: ref["key_prefix"] + suffix,
    )
    calls = []
    stream = body if body is not None else io.BytesIO(payload)
    metadata = (
        head
        if head is not None
        else {
            "ContentLength": len(payload),
            "ETag": '"saved-etag"',
            "VersionId": "saved-version",
        }
    )

    class Client:
        def head_object(self, **params):
            calls.append(("head", params))
            return metadata

        def get_object(self, **params):
            calls.append(("get", params))
            return {"Body": stream}

    @contextmanager
    def context(target):
        calls.append(("open", target))
        try:
            yield Client()
        finally:
            calls.append(("close", target))

    monkeypatch.setattr(
        app.services,
        "import_storage",
        SimpleNamespace(client_context=context),
        raising=False,
    )
    return object_key, relative, owner, stream, calls


def test_physical_read_uses_saved_full_key_and_observed_version(monkeypatch, tmp_path):
    key, relative, owner, body, calls = physical_world(monkeypatch)
    path = tmp_path / "staged"
    staged = service._materialize(
        "saved-target", owner, key, hashlib.sha256(b"saved bytes").hexdigest(), path
    )
    assert staged.object_key == key and path.read_bytes() == b"saved bytes"
    assert calls[2] == (
        "get",
        {
            "Bucket": "mask-saved",
            "Key": owner.key(relative),
            "IfMatch": '"saved-etag"',
            "VersionId": "saved-version",
        },
    )
    assert body.closed and calls[-1] == ("close", "saved-target")


def test_physical_digest_conflict_closes_body_and_client(monkeypatch, tmp_path):
    key, _, owner, body, calls = physical_world(monkeypatch)
    with pytest.raises(service.MaskFormatError, match="digest changed") as caught:
        service._materialize("target", owner, key, "0" * 64, tmp_path / "staged")
    assert caught.value.reason == "staged_object_digest_conflict"
    assert body.closed and calls[-1] == ("close", "target")


@pytest.mark.parametrize("streaming", [False, True])
def test_physical_quota_remains_bounded(monkeypatch, tmp_path, streaming):
    key, _, owner, body, calls = physical_world(
        monkeypatch, head={"ContentLength": 0 if streaming else 11}
    )
    monkeypatch.setattr(service.settings, "mask_format_temp_quota_bytes", 5)
    with pytest.raises(service.MaskFormatError) as caught:
        service._materialize("target", owner, key, "0" * 64, tmp_path / "staged")
    assert caught.value.status_code == 413
    assert any(kind == "get" for kind, _ in calls) is streaming
    if streaming:
        assert body.closed
    assert calls[-1] == ("close", "target")


def test_physical_unknown_body_close_is_not_success(monkeypatch, tmp_path):
    class UnknownClose(io.BytesIO):
        def close(self):
            super().close()
            raise RuntimeError("close unknown")

    body = UnknownClose(b"saved bytes")
    key, _, owner, _, calls = physical_world(monkeypatch, body=body)
    with pytest.raises(RuntimeError) as caught:
        service._materialize(
            "target",
            owner,
            key,
            hashlib.sha256(b"saved bytes").hexdigest(),
            tmp_path / "staged",
        )
    assert caught.value.storage_operation_unsettled and body.closed
    assert calls[-1] == ("close", "target")


def test_physical_legacy_read_does_not_invent_historical_version(monkeypatch, tmp_path):
    key, _, owner, body, calls = physical_world(monkeypatch, schema=1)
    checked = []

    def verify(key, *, bucket):
        checked.append((key, bucket))
        return {"ContentLength": 11}

    monkeypatch.setattr(service.storage_service, "verify_upload", verify)
    staged = service._materialize(
        "target",
        owner,
        key,
        hashlib.sha256(b"saved bytes").hexdigest(),
        tmp_path / "staged",
    )
    assert staged.size_bytes == 11 and body.closed
    assert checked == [(owner.key("a.json"), owner.bucket)]
    assert not any(kind == "head" for kind, _ in calls)
    assert set(next(params for kind, params in calls if kind == "get")) == {
        "Bucket",
        "Key",
    }


def test_physical_provider_failure_is_safe_and_keeps_status(monkeypatch, tmp_path):
    key, _, owner, _, calls = physical_world(monkeypatch)

    @contextmanager
    def denied(_target):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "private credentials"}},
            "HeadObject",
        )
        yield

    monkeypatch.setattr(app.services.import_storage, "client_context", denied)
    with pytest.raises(service.MaskFormatError) as caught:
        service._materialize("target", owner, key, "0" * 64, tmp_path / "staged")
    assert caught.value.status_code == 503
    assert "private" not in caught.value.message and calls == []


async def test_storage_error_conversion_preserves_unsettled_responsibility():
    async def fail():
        raise storage_access.mark_unsettled(
            ContentError("storage_operation_unsettled", 503)
        )

    with pytest.raises(service.MaskFormatError) as caught:
        await service.storage_call(fail)
    assert caught.value.status_code == 503
    assert caught.value.storage_operation_unsettled


async def test_upload_owner_is_saved_before_sign_and_keeps_first_expiry(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    from app.services import import_storage

    user, token = super_admin
    project, _, _ = await _seed_aap_import(db_session, owner_id=user.id)
    await db_session.commit()
    signed = []

    async def allocate(_db, prefix):
        return external_ref(prefix)

    async def check(_db, _owner, **_kwargs):
        return 1

    async def sign(db, owner, relative, content_type, *, expires_in):
        upload = await db.scalar(
            select(MaskFormatUpload).where(
                MaskFormatUpload.logical_object_key == owner.logical_prefix + relative
            )
        )
        assert upload is not None and upload.storage_ref == owner.ref
        assert upload.upload_expires_at - upload.created_at == timedelta(seconds=900)
        assert content_type == upload.content_type == "application/json"
        signed.append(deepcopy(owner.ref))
        return "https://mask-saved.example.test/signed"

    monkeypatch.setattr(import_storage, "allocate", allocate)
    monkeypatch.setattr(import_storage, "check_location", check)
    monkeypatch.setattr(import_storage, "prepare_upload_url", sign)
    monkeypatch.setattr(import_storage, "validate_upload_url", check)
    result = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports:upload-init",
        headers=_bearer(token),
        json={"file_name": "a b.json", "content_type": "application/json"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["object_key"].endswith("/a_b.json")
    assert 0 < result.json()["expires_in"] <= 900 and len(signed) == 1
    assert "_aap_managed" not in result.json()["object_key"]


async def test_upload_does_not_return_url_after_actor_revocation(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    from app.services import import_storage

    user, token = super_admin
    project, _, _ = await _seed_aap_import(db_session, owner_id=user.id)
    await db_session.commit()

    async def allocate(_db, prefix):
        return external_ref(prefix)

    async def check(_db, _owner, **_kwargs):
        return 1

    async def sign(*_args, **_kwargs):
        user.is_active = False
        await db_session.flush()
        return "https://must-not-return.example.test/signed"

    monkeypatch.setattr(import_storage, "allocate", allocate)
    monkeypatch.setattr(import_storage, "check_location", check)
    monkeypatch.setattr(import_storage, "prepare_upload_url", sign)
    monkeypatch.setattr(import_storage, "validate_upload_url", check)
    result = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-formats/imports:upload-init",
        headers=_bearer(token),
        json={"file_name": "a.json"},
    )
    assert result.status_code == 403, result.text
    assert "must-not-return" not in result.text


@pytest.mark.parametrize("new_owner", [False, True])
async def test_preflight_pins_upload_or_explicit_legacy_and_resume_retains_it(
    db_session,
    super_admin,
    monkeypatch,
    tmp_path,
    new_owner,
):
    from app.services import import_storage

    user, _ = super_admin
    project, _, payload = await _seed_aap_import(db_session, owner_id=user.id)
    identifier = uuid.uuid4()
    prefix = f"mask-formats/{project.id}/{user.id}/{identifier}/"
    key = prefix + "a.json"
    ref = external_ref(prefix) if new_owner else import_storage.legacy_ref(prefix)
    if new_owner:
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.add(
            MaskFormatUpload(
                id=identifier,
                project_id=project.id,
                uploader_id=user.id,
                logical_object_key=key,
                content_type="application/json",
                storage_ref=ref,
                created_at=started,
                upload_expires_at=started + timedelta(seconds=900),
            )
        )
    await db_session.commit()
    local = tmp_path / "a.json"
    local.write_bytes(payload)
    captured = []

    @asynccontextmanager
    async def materialize(
        _db, *, object_key, expected_sha256, staging_ref, observation
    ):
        captured.append(deepcopy(staging_ref))
        observation["revision"] = 1
        yield service.StagedObject(
            object_key, expected_sha256, str(local), len(payload)
        )

    async def check(*_args, **_kwargs):
        return 1

    async def no_allocation(*_args, **_kwargs):
        pytest.fail("preflight/resume must not allocate from the current binding")

    monkeypatch.setattr(service, "materialize_staged_object", materialize)
    monkeypatch.setattr(service, "check_staging", check)
    monkeypatch.setattr(import_storage, "allocate", no_allocation)
    response = await service.preflight_import(
        db_session,
        project=project,
        user_id=user.id,
        format_id="aap_json",
        staged_object_key=key,
        staged_sha256=hashlib.sha256(payload).hexdigest(),
        mapping={},
        options={"overwrite": False},
    )
    batch = await db_session.get(MaskFormatImport, response.import_id)
    assert captured == [ref] and batch.staging_ref == ref
    assert batch.staged_object_key == key
    assert response.receipt_expires_at - batch.created_at <= timedelta(
        minutes=15, seconds=1
    )
    original = (
        batch.staging_ref,
        batch.staged_sha256,
        batch.plan_digest,
        batch.mapping_digest,
        batch.options_digest,
    )
    batch.status = "failed"
    await service.resume_import(db_session, batch=batch, user_id=user.id)
    assert original == (
        batch.staging_ref,
        batch.staged_sha256,
        batch.plan_digest,
        batch.mapping_digest,
        batch.options_digest,
    )
    assert batch.status == "pending"


async def test_null_old_batch_is_legacy_even_with_current_external_binding(monkeypatch):
    from app.services import import_storage
    from app.services.dataset_assets import LEGACY_LOCATION_ID

    key = f"mask-formats/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}/a.json"

    async def no_allocation(*_args, **_kwargs):
        pytest.fail("saved old rows must not inspect the current binding")

    monkeypatch.setattr(import_storage, "allocate", no_allocation)
    owner = await service.staging_owner(None, key, None)
    assert owner.location_id == LEGACY_LOCATION_ID
    assert owner.ref["schema"] == 1 and owner.key("a.json") == key
