"""Video read capabilities retain current authority without acquiring write scope."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException

from app.db.models.annotation import Annotation
from app.schemas.annotation import AnnotationCreate, AnnotationUpdate
from app.services import prediction_originals as common
from app.services import raster_mask_objects, raster_mask_storage
from app.services import video_prediction_acceptance as accept
from app.services import video_prediction_originals as originals
from app.services import video_prediction_reads as reads
from app.services import video_prediction_references as references
from app.services.annotation import _sync_attributes_meta
from app.services.content_snapshot import ContentError
from app.services.reference_bindings import task_binding
from tests.test_video_prediction_acceptance import (
    install_flow,
    install_mask_flow,
    owned_mask,
    world,
)
from tests.test_video_prediction_authority import probe


def mask_world(origin="direct"):
    current = world(origin)
    value, reference, rle, facts = owned_mask()
    current.prediction.result = value
    current.prediction.tool_unit_id = "region"
    current.evidence["result_qualification"]["masks"] = deepcopy(facts)
    extra = {
        "original_video_input": references.public_provenance(current.evidence),
        "pipeline": {"stage_count": 1},
    }
    current.metadata.extra = {
        **extra,
        accept.PROOF_KEY: accept.persisted_qualification(
            current.prediction, extra, current.evidence
        ),
    }
    current.reference, current.rle, current.facts = reference, rle, facts
    current.geometry = deepcopy(value[0]["geometry"])
    return current


async def accepted(monkeypatch, current):
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    row = Annotation(
        id=uuid.uuid4(),
        task_id=current.task.id,
        project_id=current.task.project_id,
        user_id=current.actor.id,
        source="prediction_based",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="car",
        geometry=deepcopy(current.geometry),
        track_id=current.geometry["track_id"],
        parent_prediction_id=current.prediction.id,
        attributes={"_shape_index": 0},
        attributes_meta={},
        is_active=True,
        was_cancelled=False,
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    row.attributes_meta = {
        reads.LINEAGE_KEY: reads.annotation_lineage(
            current.db, row, current.prediction, receipt, 0
        )
    }
    current.annotation = row
    return receipt


def install_read_flow(monkeypatch, current):
    events = []
    source = SimpleNamespace(
        probe=probe(), closed=False, settled=False, unsettled=False
    )

    async def close(failure=None):
        events.append("source-close")
        source.closed = source.settled = True

    source.aclose = AsyncMock(side_effect=close)

    async def commit():
        events.append("commit")
        current.state.transaction = None

    async def metadata(db, task_id, project_id, actor_id, credentials, *, binding=None):
        assert (task_id, project_id, actor_id) == (
            current.task.id,
            current.task.project_id,
            current.actor.id,
        )
        assert credentials == current.credentials
        actual = {
            **task_binding(current.task),
            "original": current.evidence["original"],
        }
        if binding is not None and not accept._same(binding, actual):
            raise ContentError("content_binding_changed")
        if db is current.db and current.state.transaction is None:
            assert source.closed and source.settled and not source.unsettled
            events.append("final-lock")
            current.state.transaction = object()
        else:
            events.append("authority")
        return current.task, actual

    async def open_source(db, task_id, expected, *, guard, purpose, node_id):
        assert current.state.transaction is None
        assert expected == current.evidence["original"]
        assert purpose.startswith("video_mask_read_")
        events.append("source-open")
        await guard(SimpleNamespace())
        return source

    async def masks(db, value, probe, *, content=None, content_key=None):
        assert current.state.transaction is None
        assert value == [
            {"type": current.geometry["type"], "geometry": current.geometry}
        ]
        assert content_key == current.reference["object_key"]
        content[content_key] = deepcopy(current.rle)
        events.append("physical-rle-close")
        return deepcopy(current.facts)

    monkeypatch.setattr(reads, "_current", metadata)
    monkeypatch.setattr(
        common, "capture_credentials", AsyncMock(return_value=current.credentials)
    )
    monkeypatch.setattr(originals, "open_snapshot", AsyncMock(side_effect=open_source))
    monkeypatch.setattr(
        accept,
        "_candidate",
        AsyncMock(return_value=(current.prediction, current.metadata)),
    )
    monkeypatch.setattr(accept, "qualify_mask_references", AsyncMock(side_effect=masks))
    monkeypatch.setattr(accept, "lock_mask_references", AsyncMock())
    if hasattr(current, "annotation"):
        monkeypatch.setattr(
            reads, "_annotation", AsyncMock(return_value=current.annotation)
        )
    current.db.commit = AsyncMock(side_effect=commit)
    return source, events


async def qualify(current, *, annotation=False):
    if annotation:
        return await reads.qualify_annotation_read(
            current.db,
            current.task,
            current.actor,
            current.request,
            current.annotation.id,
            0,
        )
    return await reads.qualify_candidate_read(
        current.db,
        current.task,
        current.actor,
        current.request,
        current.prediction.id,
        0,
        0,
    )


@pytest.mark.parametrize("origin", ["task", "direct", "primary"])
async def test_candidate_three_origins_close_source_before_sql_and_return_cached_content(
    monkeypatch, origin
):
    current = mask_world(origin)
    source, events = install_read_flow(monkeypatch, current)
    receipt = await qualify(current)
    assert events.index("commit") < events.index("physical-rle-close")
    assert events.index("physical-rle-close") < events.index("source-open")
    assert events.index("source-close") < events.index("final-lock")
    assert source.closed and source.settled
    originals.open_snapshot.reset_mock()
    accept.qualify_mask_references.reset_mock()
    value = await reads.read_content(
        current.db, current.task, receipt, current.reference
    )
    assert value == current.rle
    value["counts"].clear()
    assert (
        await reads.read_content(current.db, current.task, receipt, current.reference)
        == current.rle
    )
    originals.open_snapshot.assert_not_awaited()
    accept.qualify_mask_references.assert_not_awaited()


@pytest.mark.parametrize("origin", ["task", "direct", "primary"])
async def test_accepted_lineage_survives_parent_deletion_and_human_mask_edit(
    monkeypatch, origin
):
    current = mask_world(origin)
    await accepted(monkeypatch, current)
    lineage = deepcopy(current.annotation.attributes_meta[reads.LINEAGE_KEY])
    current.annotation.parent_prediction_id = None
    current.annotation.version += 1
    current.annotation.geometry["keyframes"].append(
        {"frame_index": 1, "source": "manual", "mask": current.reference}
    )
    current.geometry = deepcopy(current.annotation.geometry)
    current.task.version += 1
    source, _ = install_read_flow(monkeypatch, current)
    receipt = await qualify(current, annotation=True)
    assert source.settled
    assert (
        await reads.read_content(current.db, current.task, receipt, current.reference)
        == current.rle
    )
    assert current.annotation.attributes_meta[reads.LINEAGE_KEY] == lineage
    accept._candidate.assert_not_awaited()


async def test_accepted_mask_does_not_depend_on_parent_rejection_or_generation_credentials(
    monkeypatch,
):
    current = mask_world()
    await accepted(monkeypatch, current)
    current.metadata.extra["superseded_by_prediction_id"] = str(uuid.uuid4())
    current.prediction.rejected_shape_indexes = [0]
    current.evidence["actor_id"] = str(uuid.uuid4())
    install_read_flow(monkeypatch, current)
    await qualify(current, annotation=True)
    accept._candidate.assert_not_awaited()


async def test_server_lineage_is_safe_and_cannot_be_requested_or_forged_by_source_label(
    monkeypatch,
):
    current = mask_world()
    receipt = await accepted(monkeypatch, current)
    lineage = current.annotation.attributes_meta[reads.LINEAGE_KEY]
    encoded = common.encoded(lineage)
    for private in (
        "bucket",
        "endpoint_url",
        "object_key",
        "credentials",
        "file_path",
        "facts",
    ):
        assert private not in encoded
    for model in (AnnotationCreate, AnnotationUpdate):
        assert "attributes_meta" not in model.model_fields
    changed = _sync_attributes_meta(
        {}, current.annotation.attributes_meta, {"human": "edited"}
    )
    assert changed[reads.LINEAGE_KEY] == lineage
    changed[reads.LINEAGE_KEY]["source_digest"] = "0" * 64
    assert current.annotation.attributes_meta[reads.LINEAGE_KEY] == lineage
    assert reads.LINEAGE_KEY not in _sync_attributes_meta(
        {}, {}, {reads.LINEAGE_KEY: lineage}
    )
    current.annotation.attributes_meta = {}
    with pytest.raises(ContentError, match="requires_new_request"):
        reads._lineage(current.annotation)
    with pytest.raises(ContentError, match="receipt_invalid"):
        reads.annotation_lineage(
            current.db, current.annotation, current.prediction, object(), 0
        )
    current.annotation.geometry["outside"] = [{"from": 2, "to": 3}]
    with pytest.raises(ContentError, match="receipt_invalid"):
        reads.annotation_lineage(
            current.db, current.annotation, current.prediction, receipt, 0
        )


@pytest.mark.parametrize(
    "forgery",
    [
        "issuer",
        "session",
        "transaction",
        "nested",
        "task",
        "reference",
        "accept_receipt",
    ],
)
async def test_read_receipt_is_exact_session_transaction_geometry_and_reference(
    monkeypatch, forgery
):
    current = mask_world()
    write_receipt = await accepted(monkeypatch, current)
    install_read_flow(monkeypatch, current)
    receipt = await qualify(current)
    reference = deepcopy(current.reference)
    if forgery in {"issuer", "session", "transaction"}:
        receipt = replace(receipt, **{forgery: object()})
    elif forgery == "nested":
        current.state.nested = True
    elif forgery == "task":
        receipt = replace(receipt, task_id=uuid.uuid4())
    elif forgery == "reference":
        reference["sha256"] = "0" * 64
    else:
        receipt = write_receipt
    with pytest.raises(ContentError, match="receipt_invalid"):
        await reads.read_content(current.db, current.task, receipt, reference)


async def test_read_receipt_never_grants_acceptance_or_result_write(monkeypatch):
    current = mask_world()
    install_read_flow(monkeypatch, current)
    receipt = await qualify(current)
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db, current.prediction, current.actor.id, receipt
        )
    with pytest.raises(ContentError, match="receipt_invalid"):
        await references.validate_receipt(
            current.db, current.task.id, current.task.project_id, receipt
        )


@pytest.mark.parametrize(
    "drift", ["source", "probe", "candidate", "annotation", "credentials", "owner"]
)
async def test_changed_facts_or_final_authority_never_returns_old_rle(
    monkeypatch, drift
):
    current = mask_world()
    if drift == "annotation":
        await accepted(monkeypatch, current)
    source, _ = install_read_flow(monkeypatch, current)
    original_close = source.aclose

    async def close(failure=None):
        await original_close(failure)
        if drift == "source":
            current.evidence["original"]["bucket"] = "different-owner"
        elif drift == "candidate":
            current.prediction.rejected_shape_indexes = [0]
        elif drift == "annotation":
            current.annotation.version += 1

    source.aclose = close
    if drift == "probe":
        source.probe["video"]["width"] += 1
    if drift == "credentials":
        original_current = reads._current

        async def revoked(*args, **kwargs):
            if source.closed:
                raise ContentError("api_key_authority_revoked", 403)
            return await original_current(*args, **kwargs)

        monkeypatch.setattr(reads, "_current", revoked)
    if drift == "owner":
        accept.lock_mask_references.side_effect = ContentError(
            "prediction_video_mask_changed"
        )
    with pytest.raises((ContentError, HTTPException)):
        await qualify(current, annotation=drift == "annotation")
    assert source.closed and source.settled


async def test_unknown_source_close_is_sticky_and_does_not_reach_final_locks(
    monkeypatch,
):
    current = mask_world()
    source, events = install_read_flow(monkeypatch, current)

    async def close(failure=None):
        source.unsettled = True
        raise reads.storage_access.mark_unsettled(RuntimeError("unknown close"))

    source.aclose = close
    with pytest.raises(RuntimeError, match="unknown close") as caught:
        await qualify(current)
    assert caught.value.storage_operation_unsettled
    assert "final-lock" not in events
    assert not source.settled


async def test_repeated_cancel_drains_real_close_before_releasing_read_resources(
    monkeypatch,
):
    current = mask_world()
    source, events = install_read_flow(monkeypatch, current)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(failure=None):
        entered.set()
        await release.wait()
        source.closed = source.settled = True

    source.aclose = close
    pending = asyncio.create_task(qualify(current))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and "final-lock" not in events
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert source.closed and source.settled
    assert "final-lock" not in events


async def test_current_read_key_scope_is_sufficient_without_annotation_write(
    monkeypatch,
):
    current = mask_world()
    key_id = uuid.uuid4()
    value = {
        "schema": 1,
        "actor_id": str(current.actor.id),
        "api_key_id": str(key_id),
        "key_fingerprint": "a" * 64,
        "key_expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        "jwt": None,
    }
    reauthorize = AsyncMock(return_value=current.actor)
    scope = AsyncMock()
    monkeypatch.setattr(common, "reauthorize", reauthorize)
    monkeypatch.setattr(reads, "key_authority", scope)
    await reads._credentials(current.db, value, current.actor.id)
    reauthorize.assert_awaited_once_with(current.db, value, current.actor.id, key_id)
    scope.assert_awaited_once_with(
        current.db, current.actor, key_id, "annotations:read"
    )


async def test_requested_rle_only_is_cached_while_all_masks_are_still_verified(
    monkeypatch,
):
    masks = install_mask_flow(monkeypatch)
    content = {}
    facts = await accept.qualify_mask_references(
        masks.db,
        masks.value,
        probe(),
        content=content,
        content_key=masks.reference["object_key"],
    )
    assert facts == masks.facts
    assert content == {masks.reference["object_key"]: masks.rle}
    content[masks.reference["object_key"]]["counts"].clear()
    assert masks.rle["counts"]


async def test_multi_mask_track_keeps_only_requested_rle_in_content_cache(monkeypatch):
    masks = install_mask_flow(monkeypatch)
    other_rle = {"encoding": "coco_rle", "size": [96, 128], "counts": [128 * 96]}
    other_ref = raster_mask_storage.build_rle_reference(other_rle)
    other_fact = deepcopy(masks.facts[0])
    other_fact["reference"] = other_ref
    other_fact["receipt"]["id"] = str(uuid.uuid4())
    values = {
        masks.reference["object_key"]: (masks.rle, masks.facts[0]),
        other_ref["object_key"]: (other_rle, other_fact),
    }
    masks.value[0]["geometry"]["keyframes"].append(
        {"frame_index": 1, "mask": other_ref, "source": "manual"}
    )

    async def owner(db, reference, *, lock=False):
        return deepcopy(values[reference["object_key"]][1])

    async def load(reference, *, db):
        return deepcopy(values[reference["object_key"]][0])

    monkeypatch.setattr(accept, "_mask_owner", owner)
    monkeypatch.setattr(raster_mask_objects, "load", AsyncMock(side_effect=load))
    content = {}
    facts = await accept.qualify_mask_references(
        masks.db,
        masks.value,
        probe(),
        content=content,
        content_key=other_ref["object_key"],
    )
    assert len(facts) == 2
    assert raster_mask_objects.load.await_count == 2
    assert content == {other_ref["object_key"]: other_rle}


async def test_legacy_manual_annotation_dispatch_does_not_open_current_source(
    monkeypatch,
):
    row = Annotation(parent_prediction_id=None, attributes_meta={}, source="manual")
    assert not await reads.requires_annotation_read(SimpleNamespace(), row)
    row.source = "prediction_based"
    row.attributes = {"_shape_index": 0, reads.LINEAGE_KEY: {"fake": True}}
    assert not await reads.requires_annotation_read(SimpleNamespace(), row)


def test_lineage_source_digest_changes_when_only_saved_locator_changes():
    current = mask_world()
    binding = {**task_binding(current.task), "original": current.evidence["original"]}
    proof = {
        "source_digest": common.digest(binding["original"]),
        "binding_digest": reads._binding_digest(binding),
    }
    reads._source_matches("annotation", proof, binding)
    binding["original"] = {**binding["original"], "bucket": "current-bucket-trap"}
    with pytest.raises(ContentError, match="content_binding_changed"):
        reads._source_matches("annotation", proof, binding)
