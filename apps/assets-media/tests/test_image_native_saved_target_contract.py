"""Portable Native saved-target and read-permit contract edges."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from yoao_assets_images import contract as c


def _target(output_id):
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid4()),
        "config_revision": 3,
        "binding_generation": 7,
        "endpoint_url": "https://cache.example",
        "bucket": "media-cache",
        "key_prefix": "tenant/",
        "prefix": f"tenant/image-pyramids/objects/{output_id}/",
        "storage_identity": "a" * 64,
    }


def _manifest():
    return c.ImagePyramidManifest(
        schema="aap-image-pyramid/v1",
        generation=1,
        sourceFingerprint="sha256:" + "b" * 64,
        normalizationVersion="exif-autorotate-srgb-v1",
        width=1,
        height=1,
        tileSize=512,
        overlap=1,
        format="webp",
        levels=[
            {
                "level": 0,
                "scaleFactor": 1,
                "width": 1,
                "height": 1,
                "columns": 1,
                "rows": 1,
            }
        ],
        overview={"width": 1, "height": 1, "contentDigest": "sha256:" + "c" * 64},
    )


def _native_generation(output_id, *, native=True, native_output=None):
    return c.ImagePyramidGeneration(
        asset_id=uuid4(),
        status="ready",
        required=True,
        generation=1,
        building_generation=None,
        building_status=None,
        source_generation=2,
        source_snapshot="d" * 64,
        width=1,
        height=1,
        format="webp",
        manifest=_manifest(),
        manifest_key=f"tenant/image-pyramids/objects/{output_id}/manifest.json",
        manifest_digest="sha256:" + "e" * 64,
        overview_key=f"tenant/image-pyramids/objects/{output_id}/overview.webp",
        overview_digest="sha256:" + "c" * 64,
        tile_count=1,
        retained_bytes=0,
        native=native,
        native_output=native_output,
    )


def test_saved_target_and_output_reference_require_the_enclosing_prefix():
    output_id = uuid4()
    target = c.NativePyramidSavedTarget.model_validate(_target(output_id))
    reference = c.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=60,
        fixture_owned=False,
    )
    assert reference.storage_ref.prefix.endswith(f"image-pyramids/objects/{output_id}/")

    escaped = _target(output_id)
    escaped["prefix"] = escaped["key_prefix"] + "image-pyramids/objects/other/"
    with pytest.raises(ValidationError):
        c.NativePyramidOutputReference(
            output_id=output_id,
            storage_ref=c.NativePyramidSavedTarget.model_validate(escaped),
            url_expiry_seconds=60,
            fixture_owned=False,
        )


def test_allocation_returns_the_full_saved_target():
    output_id = uuid4()
    target = c.NativePyramidSavedTarget.model_validate(_target(output_id))
    allocation = c.PyramidOutputAllocation(
        output_id=output_id,
        storage_ref=target,
        job_id=uuid4(),
        job_generation=1,
        lease_token=uuid4(),
        asset_id=uuid4(),
        generation=1,
    )
    assert allocation.storage_ref == target
    assert not hasattr(allocation, "prefix")

    escaped = _target(output_id)
    escaped["prefix"] = escaped["key_prefix"] + "image-pyramids/objects/other/"
    with pytest.raises(ValidationError):
        c.PyramidOutputAllocation(
            output_id=output_id,
            storage_ref=c.NativePyramidSavedTarget.model_validate(escaped),
            job_id=uuid4(),
            job_generation=1,
            lease_token=uuid4(),
            asset_id=uuid4(),
            generation=1,
        )


def test_ready_native_projection_requires_its_saved_output():
    output_id = uuid4()
    target = c.NativePyramidSavedTarget.model_validate(_target(output_id))
    reference = c.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=60,
        fixture_owned=False,
    )
    generation = _native_generation(output_id, native_output=reference)
    assert generation.native is True
    assert generation.native_output == reference

    with pytest.raises(ValidationError):
        _native_generation(output_id, native=True, native_output=None)
    with pytest.raises(ValidationError):
        _native_generation(output_id, native=False, native_output=reference)
    with pytest.raises(ValidationError):
        c.ImagePyramidGeneration(
            asset_id=None,
            status="not_available",
            required=False,
            generation=None,
            building_generation=None,
            building_status=None,
            width=None,
            height=None,
            format=None,
            manifest=None,
            manifest_key=None,
            manifest_digest=None,
            overview_key=None,
            overview_digest=None,
            tile_count=0,
            retained_bytes=0,
            native=True,
            native_output=None,
        )


def test_read_permit_operations_are_control_only_and_exact():
    begin = c.OPERATIONS["begin_native_pyramid_read"]
    assert begin.role == "control"
    assert begin.request is c.BeginNativePyramidReadRequest
    assert begin.result is c.NativePyramidReadPermit
    assert begin.arguments == (
        "actor_id",
        "task_id",
        "dataset_item_id",
        "generation",
        "profile_version",
        "operation_id",
        "token",
    )
    assert begin.sql_types == (
        "uuid",
        "uuid",
        "uuid",
        "integer",
        "text",
        "uuid",
        "uuid",
    )

    complete = c.OPERATIONS["complete_native_pyramid_read"]
    assert complete.role == "control"
    assert complete.request is c.CompleteNativePyramidReadRequest
    assert complete.result is c.NativePyramidReadClosure
    assert complete.arguments == ("operation_id", "token")
    assert complete.sql_types == ("uuid", "uuid")

    output_id = uuid4()
    target = c.NativePyramidSavedTarget.model_validate(_target(output_id))
    reference = c.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=60,
        fixture_owned=False,
    )
    permit = c.NativePyramidReadPermit(
        operation_id=uuid4(),
        token=uuid4(),
        output_id=output_id,
        storage_ref=target,
        pyramid=_native_generation(output_id, native_output=reference),
        url_expiry_seconds=60,
        fixture_owned=False,
    )
    request = c.BeginNativePyramidReadRequest(
        actor_id=uuid4(),
        task_id=uuid4(),
        generation=1,
        operation_id=permit.operation_id,
        token=permit.token,
    )
    c.require_scope(request, permit)

    with pytest.raises(ValueError):
        c.require_scope(request.model_copy(update={"token": uuid4()}), permit)
    with pytest.raises(ValueError):
        c.require_scope(request.model_copy(update={"generation": 2}), permit)

    closure = c.NativePyramidReadClosure(
        operation_id=permit.operation_id, token=permit.token, closed=True
    )
    c.require_scope(
        c.CompleteNativePyramidReadRequest(
            operation_id=permit.operation_id, token=permit.token
        ),
        closure,
    )
    with pytest.raises(ValueError):
        c.require_scope(
            c.CompleteNativePyramidReadRequest(
                operation_id=permit.operation_id, token=uuid4()
            ),
            closure,
        )


def test_claim_request_no_longer_accepts_a_configured_store():
    with pytest.raises(ValidationError):
        c.ClaimNativePyramidRetirementRequest(
            worker_id=uuid4(),
            store={"endpoint_url": "https://cache.example", "bucket": "media-cache"},
        )


def test_native_member_receipt_keeps_bounded_raw_receipts():
    operation_id = uuid4()
    literal = c.PyramidMemberReceipt(
        operation_id=operation_id, etag='"etag"', version_id="null"
    )
    assert literal.version_id == "null"
    assert (
        c.PyramidMemberReceipt(operation_id=operation_id, etag='"etag"').version_id
        is None
    )
    c.PyramidMemberReceipt(
        operation_id=operation_id,
        etag='"' + "a" * 1022 + '"',
        version_id="v" * 1024,
    )
    for rejected in (
        {"etag": '""'},
        {"etag": '"' + "a" * 1023 + '"'},
        {"etag": '"etag"', "version_id": ""},
        {"etag": '"etag"', "version_id": "v" * 1025},
    ):
        with pytest.raises(ValidationError):
            c.PyramidMemberReceipt(operation_id=operation_id, **rejected)


def test_written_member_closure_is_the_only_abandon_extension():
    closure = c.PyramidMemberAbandon(operation_id=uuid4(), state="written")
    assert closure.state == "written"
    for state in ("not_written", "unknown"):
        assert c.PyramidMemberAbandon(operation_id=uuid4(), state=state).state == state
    with pytest.raises(ValidationError):
        c.PyramidMemberAbandon(operation_id=uuid4(), state="verified")
