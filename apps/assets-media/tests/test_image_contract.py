from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_images import contract as c
from aap_assets_images.geometry import build_manifest
from aap_assets_images.policy import ImagePolicy


def image_source(*, owner=None, task_id=None, dataset_id=None, item_id=None):
    owner = owner or c.ImageOwner(kind="dataset_item", id=item_id or uuid4())
    if owner.kind == "dataset_item":
        item_id = owner.id if item_id is None else item_id
        dataset_id = dataset_id or uuid4()
    else:
        task_id = owner.id if task_id is None else task_id
        item_id = None
        dataset_id = None
    return c.ImageSource(
        owner=owner,
        task_id=task_id,
        project_id=uuid4(),
        dataset_id=dataset_id,
        item_id=item_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "observed", "byte_size": 128, "version_id": None},
        source_drifted=False,
        upload_ready=True,
        file_path="source/images/image.png",
        file_name="image.png",
        file_type="image",
        bucket_kind="datasets" if owner.kind == "dataset_item" else "legacy",
        file_size=128,
        content_hash=None,
        width=2,
        height=1,
        thumbnail_path="thumbnails/image.webp",
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        thumbnail_source_generation=1,
        thumbnail_error=None,
    )


def manifest(
    *,
    generation=3,
    source_sha="b" * 64,
    overview_sha="c" * 64,
):
    return c.ImagePyramidManifest.model_validate(
        build_manifest(
            generation=generation,
            source_fingerprint="sha256:" + source_sha,
            width=2,
            height=1,
            overview_width=2,
            overview_height=1,
            overview_digest="sha256:" + overview_sha,
            policy=ImagePolicy(),
        )
    )


def pyramid_result(prefix, *, source_identity=None, source_sha="b" * 64):
    source_identity = source_identity or c.ObjectIdentity(
        etag="observed", byte_size=128
    )
    manifest_value = manifest(source_sha=source_sha)
    tiles = [
        c.ImageTileArtifact(
            level=level,
            x=0,
            y=0,
            artifact={
                "key": f"{prefix}tiles/{level}/0/0.webp",
                "byte_size": 10,
                "sha256": str(level + 1) * 64,
            },
        )
        for level in range(2)
    ]
    return c.PyramidResult(
        source_identity=source_identity,
        source_sha256=source_sha,
        manifest=manifest_value,
        manifest_artifact={
            "key": prefix + "manifest.json",
            "byte_size": 11,
            "sha256": "d" * 64,
        },
        overview_artifact={
            "key": prefix + "overview.webp",
            "byte_size": 12,
            "sha256": "c" * 64,
        },
        tile_artifacts=tiles,
        tile_count=2,
        retained_bytes=43,
    )


def test_actual_geometry_manifest_wire_keeps_prefixed_sha256_digests():
    generated = build_manifest(
        generation=3,
        source_fingerprint="sha256:" + "b" * 64,
        width=2,
        height=1,
        overview_width=2,
        overview_height=1,
        overview_digest="sha256:" + "c" * 64,
        policy=ImagePolicy(),
    )
    parsed = c.ImagePyramidManifest.model_validate(generated)
    assert parsed.model_dump(mode="json", by_alias=True) == generated
    assert parsed.source_fingerprint == "sha256:" + "b" * 64
    assert parsed.overview.content_digest == "sha256:" + "c" * 64


def test_image_schema_operation_signatures_and_http_artifacts_are_frozen():
    source_root = Path(__file__).parents[1] / "src/aap_assets_images"
    assert (
        json.loads((source_root / "protocol.schema.json").read_text())
        == c.contract_document()
    )
    assert len(c.OPERATIONS) == 22
    assert {
        role: sum(operation.role == role for operation in c.OPERATIONS.values())
        for role in ("control", "system", "worker", "cleanup")
    } == {"control": 9, "system": 5, "worker": 6, "cleanup": 2}
    assert set(c.TaskImageSourceFacts.model_fields) == {
        "task_id",
        "project_id",
        "dataset_item_id",
        "file_type",
        "file_path",
        "file_name",
        "created_at",
        "source_generation",
        "source_snapshot",
        "object_identity",
        "source_drifted",
        "upload_ready",
        "thumbnail_path",
        "blurhash",
        "thumbnail_source_generation",
    }
    for operation in c.OPERATIONS.values():
        assert set(operation.arguments) <= operation.request.model_fields.keys()
        assert len(operation.arguments) == len(operation.sql_types)
    document_path = source_root / "openapi.snapshot.json"
    document = json.loads(document_path.read_text())
    from aap_assets_images.http_contract import http_document

    assert document == http_document()
    assert len(document["paths"]) == 20
    assert "/v1/images/commands/claim_image_media" not in document["paths"]
    assert "/v1/images/commands/claim_image_garbage" not in document["paths"]
    assert "/v1/images/asset-references" in document["paths"]


def test_source_selector_and_owner_projection_are_strict():
    with pytest.raises(ValidationError):
        c.ImageSourceSelector()
    with pytest.raises(ValidationError):
        c.ImageSourceSelector(task_id=uuid4(), dataset_item_id=uuid4())
    linked_task = uuid4()
    item = uuid4()
    linked = image_source(
        owner=c.ImageOwner(kind="dataset_item", id=item),
        task_id=linked_task,
        item_id=item,
    )
    c.require_scope(
        c.ReadImageSourceRequest(actor_id=uuid4(), task_id=linked_task), linked
    )
    with pytest.raises(ValueError, match="escaped its selector"):
        c.require_scope(
            c.ReadImageSourceRequest(actor_id=uuid4(), task_id=uuid4()), linked
        )
    with pytest.raises(ValueError, match="escaped its selector"):
        c.require_scope(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=item), linked
        )
    legacy_task = uuid4()
    legacy = image_source(owner=c.ImageOwner(kind="task", id=legacy_task))
    assert legacy.bucket_kind == "legacy"
    assert legacy.item_id is None
    assert legacy.thumbnail_current
    with pytest.raises(ValidationError):
        c.ImageSource.model_validate(
            {**legacy.model_dump(), "file_path": "../foreign.png"}
        )


def test_labeling_task_projection_keeps_upload_readiness_separate():
    facts = c.TaskImageSourceFacts(
        task_id=uuid4(),
        project_id=uuid4(),
        dataset_item_id=None,
        file_type="image",
        file_path="tasks/1/source.png",
        file_name="source.png",
        created_at=datetime.now(timezone.utc),
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity=None,
        source_drifted=False,
        upload_ready=False,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
    )
    assert facts.upload_ready is False
    assert facts.source_generation == 1


def test_actor_acceptance_captures_the_exact_image_policy():
    policy = ImagePolicy(max_source_bytes=123456)
    request = c.PrepareImageMediaRequest(
        operation_id=uuid4(),
        actor_id=uuid4(),
        dataset_item_id=uuid4(),
        purpose="thumbnail",
        expected_source_snapshot="a" * 64,
        policy=policy,
    )
    assert request.policy == policy
    spec = c.ThumbnailWorkSpec(expected_source_snapshot="a" * 64, policy=policy)
    owner = c.ImageOwner(kind="dataset_item", id=request.dataset_item_id)
    source = image_source(owner=owner, item_id=request.dataset_item_id)
    work = c.ImageWorkReference(
        job_id=uuid4(),
        generation=1,
        owner=owner,
        source_generation=1,
        kind="thumbnail",
        status="pending",
    )
    accepted = c.AcceptedImageWork(
        operation_id=request.operation_id, source=source, work=work, spec=spec
    )
    c.require_scope(request, accepted)
    with pytest.raises(ValueError, match="captured policy"):
        c.require_scope(
            request.model_copy(update={"policy": ImagePolicy(max_source_bytes=123457)}),
            accepted,
        )


def test_pyramid_claim_and_result_are_bound_to_generation_grid_and_attempt_prefix():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    source = image_source(owner=owner, item_id=owner.id)
    job = uuid4()
    lease = uuid4()
    asset = uuid4()
    prefix = f"image-pyramids/{asset}/g3/a{lease}/"
    claim = c.ImageClaim(
        job_id=job,
        generation=2,
        lease_token=lease,
        lease_expires_at=datetime.now(timezone.utc),
        owner=owner,
        source=source,
        spec=c.PyramidWorkSpec(expected_source_snapshot=source.source_snapshot),
        output_prefix=prefix,
        pyramid_asset_id=asset,
        pyramid_generation=3,
    )
    result = pyramid_result(prefix, source_identity=source.object_identity)
    c.require_result_scope(claim, result)
    with pytest.raises(ValueError, match="escaped its attempt prefix"):
        c.require_result_scope(
            claim,
            result.model_copy(
                update={
                    "overview_artifact": result.overview_artifact.model_copy(
                        update={"key": "foreign/overview.webp"}
                    )
                }
            ),
        )
    with pytest.raises(ValidationError, match="tile count"):
        c.PyramidResult.model_validate(
            {
                **result.model_dump(),
                "tile_artifacts": [
                    tile.model_dump() for tile in result.tile_artifacts[:1]
                ],
                "tile_count": 1,
                "retained_bytes": 33,
            }
        )
    with pytest.raises(ValidationError):
        c.ImageClaim(
            job_id=job,
            generation=2,
            lease_token=lease,
            lease_expires_at=datetime.now(timezone.utc),
            owner=owner,
            source=source,
            spec=c.PyramidWorkSpec(expected_source_snapshot=source.source_snapshot),
            output_prefix=f"image-pyramids/{asset}/g3/a{uuid4()}/",
            pyramid_asset_id=asset,
            pyramid_generation=3,
        )


def test_backfill_selection_cursor_and_reconcile_candidate_scope_are_bounded():
    with pytest.raises(ValidationError):
        c.ImageBackfillSelection(owner_kind="dataset_item", purpose="pyramid")
    with pytest.raises(ValidationError, match="Dataset ID"):
        c.ImageBackfillSelection(
            owner_kind="task", dataset_id=uuid4(), purpose="pyramid"
        )
    task_backfill = c.ImageBackfillSelection(
        owner_kind="task", project_id=uuid4(), purpose="pyramid"
    )
    assert task_backfill.owner_kind == "task"
    selection = c.ImageBackfillSelection(
        owner_kind="dataset_item", dataset_id=uuid4(), purpose="pyramid"
    )
    with pytest.raises(ValidationError):
        c.ScanImageBackfillRequest(
            selection=selection, after_owner_kind="task", after_owner_id=uuid4()
        )
    with pytest.raises(ValidationError):
        c.ReconcileImageMediaRequest(
            candidates=[
                {
                    "asset_id": uuid4(),
                    "generation": 1,
                    "last_modified": datetime.now(timezone.utc),
                }
            ]
            * 2
        )


def test_garbage_lease_accepts_only_canonical_scopes_and_reason():
    asset = uuid4()
    lease = uuid4()
    item = c.ImageGarbageItem(
        garbage_id=uuid4(),
        lease_token=lease,
        prefix=f"image-pyramids/{asset}/g2/a{lease}/",
    )
    assert item.prefix.endswith("/")
    deleted_asset = c.ImageGarbageItem(
        garbage_id=uuid4(), lease_token=lease, prefix=f"image-pyramids/{asset}/"
    )
    assert deleted_asset.prefix == f"image-pyramids/{asset}/"
    with pytest.raises(ValidationError):
        c.ImageGarbageItem(
            garbage_id=uuid4(),
            lease_token=lease,
            prefix=f"image-pyramids/{asset}/foreign/",
        )


def test_strict_envelope_budget_and_manifest_legacy_wire_names():
    source = image_source()
    body = {
        "schema": c.IMAGE_SCHEMA,
        "ok": True,
        "data": source.model_dump(mode="json"),
    }
    assert c.decode_reply(body, c.ImageSource) == source
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        c.decode_reply({**body, "unexpected": True}, c.ImageSource)
    oversized = " " * (c.REPLY_BYTES + 1)
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        c.decode_reply(oversized, c.ImageSource)
    serialized = manifest().model_dump(mode="json", by_alias=True)
    assert serialized["schema"] == c.PYRAMID_SCHEMA
    assert serialized["tileSize"] == 512
    assert "tile_size" not in serialized
