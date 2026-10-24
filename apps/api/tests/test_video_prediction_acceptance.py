"""Private video adoption proofs and pre-lock physical settlement contracts."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from app.db.models.prediction import Prediction, PredictionMeta
from app.services import prediction_originals as common
from app.services import raster_mask_objects, raster_mask_storage, storage_access
from app.services import video_prediction_acceptance as accept
from app.services import video_prediction_originals as originals
from app.services import video_prediction_references as references
from app.services.content_snapshot import ContentError
from app.services.reference_bindings import task_binding
from tests.test_video_prediction_authority import (
    evidence as generation_evidence,
    jwt_value,
    probe,
    setup_job,
    task_for,
)
from tests.test_video_prediction_originals import source as canonical_source


def shape(index=0):
    return {
        "type": "video_track_bbox",
        "class_name": "car",
        "tool_unit_id": "bbox",
        "geometry": {
            "type": "video_track_bbox",
            "track_id": f"trk_{index}",
            "keyframes": [
                {
                    "frame_index": index,
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    "source": "prediction",
                }
            ],
            "outside": [],
        },
        "confidence": 0.8,
        "attributes": {},
    }


def world(origin="direct"):
    generated = setup_job()
    source = canonical_source(origin)
    task = task_for(source)
    evidence = {
        **generation_evidence(generated),
        **task_binding(task),
        "original": originals.source_receipt(source),
        "result_qualification": {
            "schema": 1,
            "stages": [
                {"stage": 7, "raw_digest": "a" * 64, "converted_digest": "b" * 64}
            ],
            "masks": [],
        },
    }
    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=task.project_id,
        created_at=datetime.now(timezone.utc),
        source="ml_backend",
        tool_unit_id="bbox",
        score=0.8,
        model_version="declared",
        result=[shape(), shape(1)],
        rejected_shape_indexes=[],
    )
    extra = {
        "original_video_input": references.public_provenance(evidence),
        "pipeline": {"stage_count": 1},
    }
    proof = accept.persisted_qualification(prediction, extra, evidence)
    metadata = PredictionMeta(
        id=uuid.uuid4(),
        prediction_id=prediction.id,
        prediction_created_at=prediction.created_at,
        extra={**extra, accept.PROOF_KEY: proof},
    )
    actor = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    state = SimpleNamespace(transaction=object(), nested=False)
    db = SimpleNamespace(
        sync_session=SimpleNamespace(
            get_transaction=lambda: state.transaction,
            in_nested_transaction=lambda: state.nested,
        ),
        in_transaction=lambda: state.transaction is not None,
    )
    request = SimpleNamespace(
        query_params={}, body=AsyncMock(return_value=b""), json=AsyncMock()
    )
    return SimpleNamespace(
        task=task,
        actor=actor,
        evidence=evidence,
        prediction=prediction,
        metadata=metadata,
        source=source,
        db=db,
        state=state,
        request=request,
        credentials=jwt_value(actor.id),
        consumed=[],
    )


@pytest.mark.parametrize("origin", ["task", "direct", "primary"])
def test_persisted_proof_keeps_full_first_source_and_adaptation_without_credentials(
    origin,
):
    current = world(origin)
    proof = current.metadata.extra[accept.PROOF_KEY]
    assert accept._qualified(current.prediction, current.metadata) == current.evidence
    assert (
        proof["evidence"]["original"]["facts"] == current.evidence["original"]["facts"]
    )
    assert (
        proof["evidence"]["result_qualification"]
        == current.evidence["result_qualification"]
    )
    assert "credentials" not in common.encoded(proof)
    assert "bucket" not in common.encoded(
        current.metadata.extra["original_video_input"]
    )
    current.evidence["original"]["bucket"] = "changed"
    assert proof["evidence"]["original"]["bucket"] == "saved-annotations"


@pytest.mark.parametrize(
    "drift",
    [
        "id",
        "created",
        "task",
        "project",
        "source",
        "tool",
        "model",
        "score",
        "result",
        "pipeline",
        "public",
        "private",
        "adaptation",
    ],
)
def test_persisted_candidate_rejects_factual_or_metadata_drift(drift):
    current = world()
    prediction, extra = current.prediction, current.metadata.extra
    if drift in {"id", "task", "project"}:
        setattr(
            prediction,
            {"id": "id", "task": "task_id", "project": "project_id"}[drift],
            uuid.uuid4(),
        )
    elif drift == "created":
        prediction.created_at += timedelta(seconds=1)
    elif drift == "source":
        prediction.source = "external_import"
    elif drift == "tool":
        prediction.tool_unit_id = "region"
    elif drift == "model":
        prediction.model_version = "different"
    elif drift == "score":
        prediction.score = 0.2
    elif drift == "result":
        prediction.result[0]["geometry"]["keyframes"][0]["bbox"]["w"] = 0.1
    elif drift == "pipeline":
        extra["pipeline"]["stage_count"] = 2
    elif drift == "public":
        extra["original_video_input"]["probe"]["video"]["width"] = 1
    elif drift == "private":
        extra[accept.PROOF_KEY]["evidence"]["original"]["bucket"] = "current-bucket"
    else:
        extra[accept.PROOF_KEY]["evidence"]["result_qualification"]["stages"][0][
            "raw_digest"
        ] = "c" * 64
    with pytest.raises(ContentError):
        accept._qualified(prediction, current.metadata)


def test_superseding_link_is_mutable_history_but_blocks_old_candidate_adoption():
    current = world()
    before = common.digest(
        accept._candidate_value(current.prediction, current.metadata.extra)
    )
    current.metadata.extra["superseded_by_prediction_id"] = str(uuid.uuid4())
    assert (
        common.digest(
            accept._candidate_value(current.prediction, current.metadata.extra)
        )
        == before
    )
    with pytest.raises(ContentError, match="candidate_superseded"):
        accept._qualified(current.prediction, current.metadata)


def test_registry_set_null_and_expired_generation_authority_do_not_revoke_stored_candidate():
    current = world()
    current.prediction.ml_backend_id = current.prediction.ml_backend_pool_id = None
    assert accept._qualified(current.prediction, current.metadata) == current.evidence
    assert current.actor.id != uuid.UUID(current.evidence["actor_id"])


@pytest.mark.parametrize("missing", [accept.PROOF_KEY, "original_video_input"])
def test_historical_candidates_do_not_acquire_current_source_proof(missing):
    current = world()
    del current.metadata.extra[missing]
    with pytest.raises(ContentError):
        accept._qualified(current.prediction, current.metadata)


def test_generation_can_not_reseal_an_existing_private_proof_or_unprojected_public_data():
    current = world()
    with pytest.raises(ContentError):
        accept.persisted_qualification(
            current.prediction, current.metadata.extra, current.evidence
        )
    extra = {"original_video_input": deepcopy(current.evidence)}
    with pytest.raises(ContentError):
        accept.persisted_qualification(current.prediction, extra, current.evidence)


def frame():
    return {
        "type": "video_bbox",
        "class_name": "car",
        "geometry": {
            "type": "video_bbox",
            "frame_index": 3,
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "frame",
        "external",
        "track",
        "raw_sam",
        "mixed",
        "empty",
        "proof",
        "public",
        "malformed",
        "nonfinite",
    ],
)
def test_dispatch_preserves_only_strict_existing_frame_and_external_contracts(case):
    prediction = SimpleNamespace(source="ml_backend", result=[frame()])
    metadata = SimpleNamespace(extra={})
    if case == "external":
        prediction.source, prediction.result = "external_import", [shape()]
    elif case == "track":
        prediction.result = [shape()]
    elif case == "raw_sam":
        prediction.result = [{"frame_index": 3, "geometry": {"type": "bbox"}}]
    elif case == "mixed":
        prediction.result.append(shape())
    elif case == "empty":
        prediction.result = []
    elif case in {"proof", "public"}:
        metadata.extra[
            accept.PROOF_KEY if case == "proof" else "original_video_input"
        ] = None
    elif case == "malformed":
        prediction.result[0]["geometry"]["frame_index"] = True
    elif case == "nonfinite":
        prediction.result[0]["geometry"]["w"] = float("nan")
    assert accept.requires_qualification(prediction, metadata) == (
        case not in {"frame", "external"}
    )


@pytest.mark.parametrize("index", [True, -1, "0", 0.5])
def test_selection_refuses_type_coercion_before_source_read(index):
    with pytest.raises(ValueError):
        accept.selection_value(index)


def test_selection_freezes_reviewed_overrides_and_segment():
    attributes, segment_id = {"color": ["red"]}, uuid.uuid4()
    selected = accept.selection_value(1, "car", attributes, segment_id)
    attributes["color"].clear()
    assert selected == {
        "shape_index": 1,
        "override_class_name": "car",
        "attribute_overrides": {"color": ["red"]},
        "video_segment_id": str(segment_id),
    }


def consumed_db(values):
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: values)
            )
        )
    )


async def test_partial_adoption_then_all_selects_only_remaining_shapes():
    current = world()
    db = consumed_db(["0"])
    assert await accept._selected(db, current.prediction, accept.selection_value()) == [
        1
    ]
    with pytest.raises(ContentError, match="already_accepted"):
        await accept._selected(db, current.prediction, accept.selection_value(0))
    assert await accept._selected(
        db, current.prediction, accept.selection_value(1)
    ) == [1]
    current.prediction.rejected_shape_indexes = [1]
    with pytest.raises(ContentError):
        await accept._selected(db, current.prediction, accept.selection_value())


async def test_selected_rejected_out_of_range_and_malformed_history_refuse():
    current = world()
    current.prediction.rejected_shape_indexes = [0]
    with pytest.raises(ContentError, match="candidate_rejected"):
        await accept._selected(
            consumed_db([]), current.prediction, accept.selection_value(0)
        )
    with pytest.raises(HTTPException) as caught:
        await accept._selected(
            consumed_db([]), current.prediction, accept.selection_value(2)
        )
    assert caught.value.status_code == 404
    with pytest.raises(ContentError):
        await accept._selected(
            consumed_db([None]), current.prediction, accept.selection_value()
        )


def install_flow(monkeypatch, current):
    events = []
    source = SimpleNamespace(
        probe=probe(), closed=False, settled=False, unsettled=False
    )

    async def close(failure=None):
        events.append("source-close")
        source.closed = source.settled = True

    source.aclose = AsyncMock(side_effect=close)

    async def commit():
        events.append("initial-commit")
        current.state.transaction = None

    async def check(
        db, task_id, project_id, actor_id, credentials, *, binding=None, final=False
    ):
        assert (
            task_id == current.task.id
            and project_id == current.task.project_id
            and actor_id == current.actor.id
        )
        assert credentials == current.credentials
        if final:
            assert source.closed and source.settled and not source.unsettled
            events.append("final-lock")
            current.state.transaction = current.state.transaction or object()
        else:
            events.append("current-authority")
        value = {**task_binding(current.task), "original": current.evidence["original"]}
        if binding is not None and not accept._same(value, binding):
            raise ContentError("content_binding_changed")
        return current.task, current.actor, value

    async def opening(db, task_id, expected, *, guard, purpose, node_id):
        assert current.state.transaction is None
        assert expected == current.evidence["original"]
        assert purpose.startswith("video_accept_") and node_id
        events.append("source-open")
        await guard(SimpleNamespace())
        return source

    async def selected(db, prediction, selection):
        if selection["shape_index"] is not None:
            return [selection["shape_index"]]
        return [
            index
            for index in range(len(prediction.result))
            if index not in current.consumed
        ]

    monkeypatch.setattr(
        accept,
        "_candidate",
        AsyncMock(return_value=(current.prediction, current.metadata)),
    )
    monkeypatch.setattr(
        common, "capture_credentials", AsyncMock(return_value=current.credentials)
    )
    monkeypatch.setattr(accept, "_current", check)
    monkeypatch.setattr(accept, "_selected", AsyncMock(side_effect=selected))
    monkeypatch.setattr(accept, "_segment", AsyncMock())
    monkeypatch.setattr(accept, "qualify_mask_references", AsyncMock(return_value=[]))
    monkeypatch.setattr(accept, "lock_mask_references", AsyncMock())
    monkeypatch.setattr(originals, "open_snapshot", opening)
    current.db.commit = AsyncMock(side_effect=commit)
    return source, events


@pytest.mark.parametrize("origin", ["task", "direct", "primary"])
async def test_current_caller_and_changed_task_version_can_adopt_after_real_close(
    monkeypatch, origin
):
    current = world(origin)
    current.task.version += 1
    source, events = install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    assert (
        events.index("initial-commit")
        < events.index("source-open")
        < events.index("source-close")
        < events.index("final-lock")
    )
    assert source.closed and source.settled
    assert accept.selected_shape_indexes(receipt) == [0, 1]
    assert json_value(receipt.credentials) == current.credentials
    assert json_value(receipt.binding)["version"] == current.task.version
    assert json_value(receipt.evidence)["actor_id"] != str(current.actor.id)
    result = await accept.validate_acceptance(
        current.db,
        current.prediction,
        current.actor.id,
        receipt,
        selection=accept.selection_value(),
    )
    assert "bucket" not in common.encoded(result)
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(1),
        )


def json_value(value):
    import json

    return json.loads(value)


@pytest.mark.parametrize(
    "forgery",
    [
        "type",
        "issuer",
        "session",
        "transaction",
        "actor",
        "prediction",
        "task",
        "project",
        "nested",
    ],
)
async def test_acceptance_receipt_has_exact_issuer_transaction_actor_candidate_scope(
    monkeypatch, forgery
):
    current = world()
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    if forgery == "type":
        receipt = object()
    elif forgery == "nested":
        current.state.nested = True
    else:
        name = {
            "issuer": "issuer",
            "session": "session",
            "transaction": "transaction",
            "actor": "actor_id",
            "prediction": "prediction_id",
            "task": "task_id",
            "project": "project_id",
        }[forgery]
        receipt = replace(
            receipt,
            **{
                name: object()
                if forgery in {"issuer", "session", "transaction"}
                else uuid.uuid4()
            },
        )
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(),
        )


async def test_final_receipt_cannot_be_reused_after_commit_or_new_adoption(monkeypatch):
    current = world()
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    current.consumed.append(0)
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(),
        )
    current.state.transaction = object()
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(),
        )


async def test_geometry_receipt_only_authorizes_its_reviewed_geometry(monkeypatch):
    current = world()
    current.request.query_params = {"shape_index": "1"}
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    assert accept.selected_shape_indexes(receipt) == [1]
    await accept.validate_geometry_receipt(
        current.db, current.task, receipt, current.prediction.result[1]["geometry"]
    )
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_geometry_receipt(
            current.db, current.task, receipt, current.prediction.result[0]["geometry"]
        )


async def test_source_close_unknown_is_sticky_and_never_reaches_final_locks(
    monkeypatch,
):
    current = world()
    source, events = install_flow(monkeypatch, current)

    async def close(failure=None):
        source.unsettled = True
        raise storage_access.mark_unsettled(RuntimeError("unknown source close"))

    source.aclose = AsyncMock(side_effect=close)
    with pytest.raises(RuntimeError, match="unknown source close") as caught:
        await accept.qualify_acceptance(
            current.db,
            current.task,
            current.actor,
            current.request,
            current.prediction.id,
        )
    assert caught.value.storage_operation_unsettled
    assert "final-lock" not in events and not source.settled


async def test_repeated_cancel_waits_until_source_really_closes_and_never_mints_receipt(
    monkeypatch,
):
    current = world()
    source, events = install_flow(monkeypatch, current)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(failure=None):
        entered.set()
        await release.wait()
        source.closed = source.settled = True

    source.aclose = close
    pending = asyncio.create_task(
        accept.qualify_acceptance(
            current.db,
            current.task,
            current.actor,
            current.request,
            current.prediction.id,
        )
    )
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and "final-lock" not in events
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert source.closed and source.settled and "final-lock" not in events


async def test_probe_drift_closes_source_without_final_authority(monkeypatch):
    current = world()
    source, events = install_flow(monkeypatch, current)
    source.probe["video"]["width"] += 1
    with pytest.raises(ContentError, match="probe_changed"):
        await accept.qualify_acceptance(
            current.db,
            current.task,
            current.actor,
            current.request,
            current.prediction.id,
        )
    assert source.closed and source.settled and "final-lock" not in events


async def test_write_recheck_uses_current_credentials_after_own_new_rows(monkeypatch):
    current = world()
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    current.consumed.extend([0, 1])
    reauthorize = AsyncMock(return_value=current.actor)
    key = AsyncMock()
    monkeypatch.setattr(common, "reauthorize", reauthorize)
    monkeypatch.setattr(accept, "key_authority", key)
    await accept.recheck_credentials(current.db, receipt)
    reauthorize.assert_awaited_once_with(
        current.db, current.credentials, current.actor.id, None
    )
    key.assert_awaited_once_with(current.db, current.actor, None, "annotations:write")


async def test_expired_current_credential_refuses_before_reauthorization(monkeypatch):
    current = world()
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    expires = datetime.fromisoformat(current.credentials["jwt"]["expires_at"])

    class Clock:
        @classmethod
        def now(cls, tz):
            return expires + timedelta(seconds=1)

    monkeypatch.setattr(common, "datetime", Clock)
    reauthorize = AsyncMock()
    monkeypatch.setattr(common, "reauthorize", reauthorize)
    with pytest.raises(ContentError):
        await accept.recheck_credentials(current.db, receipt)
    reauthorize.assert_not_awaited()


async def test_candidate_sql_guards_json_in_same_statement_before_hydration():
    captured = []

    async def execute(statement):
        captured.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(one_or_none=lambda: (object(), False, None, None))

    with pytest.raises(ContentError, match="budget_exceeded"):
        await accept._bounded_row(
            SimpleNamespace(execute=execute),
            Prediction,
            uuid.uuid4(),
            ("result", "rejected_shape_indexes"),
            lock=True,
        )
    assert len(captured) == 1
    assert "CASE WHEN" in captured[0] and "octet_length" in captured[0]
    assert "FOR SHARE NOWAIT" in captured[0]


@pytest.mark.parametrize("drift", ["version", "owner", "gate", "batch"])
async def test_final_metadata_binding_rejects_same_sha_new_owner_and_request_races(
    monkeypatch, drift
):
    current = world()
    binding = {**task_binding(current.task), "original": current.evidence["original"]}
    monkeypatch.setattr(accept, "lock_actor_project_authority", AsyncMock())
    monkeypatch.setattr(accept, "lock_actor_scope", AsyncMock())
    monkeypatch.setattr(accept, "_credentials", AsyncMock(return_value=current.actor))
    monkeypatch.setattr(accept, "_authorize", AsyncMock())
    monkeypatch.setattr(common, "locked_task", AsyncMock(return_value=current.task))
    source_value = current.evidence["original"]
    if drift == "version":
        current.task.version += 1
    elif drift == "batch":
        current.task.batch_id = uuid.uuid4()
    elif drift == "gate":
        source_value = originals.source_receipt(
            replace(
                current.source,
                maintenance_revision=current.source.maintenance_revision + 1,
            )
        )
    else:
        identity = uuid.uuid4()
        source_value = originals.source_receipt(
            replace(
                current.source,
                content_id=identity,
                ownership_facts=replace(current.source.ownership_facts, id=identity),
                replica_facts=replace(current.source.replica_facts, asset_id=identity),
            )
        )
        assert source_value["sha256"] == binding["original"]["sha256"]
    monkeypatch.setattr(
        originals, "managed_binding", AsyncMock(return_value=source_value)
    )
    with pytest.raises(ContentError, match="content_binding_changed"):
        await accept._current(
            current.db,
            current.task.id,
            current.task.project_id,
            current.actor.id,
            current.credentials,
            binding=binding,
        )


async def test_current_key_check_uses_only_annotation_write_and_original_current_key(
    monkeypatch,
):
    current = world()
    key_id = uuid.uuid4()
    current_key = {
        "schema": 1,
        "actor_id": str(current.actor.id),
        "api_key_id": str(key_id),
        "key_fingerprint": "d" * 64,
        "key_expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        "jwt": None,
    }
    reauthorize = AsyncMock(return_value=current.actor)
    scopes = AsyncMock()
    monkeypatch.setattr(common, "reauthorize", reauthorize)
    monkeypatch.setattr(accept, "key_authority", scopes)
    assert (
        await accept._credentials(current.db, current_key, current.actor.id)
        is current.actor
    )
    reauthorize.assert_awaited_once_with(
        current.db, current_key, current.actor.id, key_id
    )
    scopes.assert_awaited_once_with(
        current.db, current.actor, key_id, "annotations:write"
    )


def owned_mask():
    rle = {"encoding": "coco_rle", "size": [96, 128], "counts": [0, 128 * 96]}
    reference = raster_mask_storage.build_rle_reference(rle)
    value = [
        {
            "type": "video_track_mask",
            "geometry": {
                "type": "video_track_mask",
                "track_id": "trk_m",
                "keyframes": [
                    {"frame_index": 0, "mask": reference, "source": "prediction"}
                ],
                "outside": [],
            },
        }
    ]
    facts = [
        {
            "reference": reference,
            "receipt": {
                "id": str(uuid.uuid4()),
                "storage_ref": {"bucket": "saved", "endpoint_url": "http://saved.test"},
            },
            "gate_revision": 7,
        }
    ]
    return value, reference, rle, facts


def install_mask_flow(monkeypatch):
    value, reference, rle, facts = owned_mask()
    events = []
    owned = SimpleNamespace(
        rollback=AsyncMock(side_effect=lambda: events.append("rollback"))
    )

    @asynccontextmanager
    async def session(db):
        events.append("session")
        try:
            yield owned
        finally:
            events.append("session-close")

    async def owner(db, reference, *, lock=False):
        events.append("locked-owner" if lock else "owner")
        return deepcopy(facts[0])

    async def load(reference, *, db):
        assert events[-1] == "rollback"
        events.append("physical-load-close")
        return deepcopy(rle)

    monkeypatch.setattr(accept, "_mask_session", session)
    monkeypatch.setattr(accept, "_mask_owner", owner)
    monkeypatch.setattr(raster_mask_objects, "load", AsyncMock(side_effect=load))
    return SimpleNamespace(
        value=value,
        reference=reference,
        rle=rle,
        facts=facts,
        events=events,
        db=SimpleNamespace(in_transaction=lambda: False),
        owned=owned,
    )


async def test_real_rle_verification_freezes_same_owner_and_releases_short_sessions(
    monkeypatch,
):
    masks = install_mask_flow(monkeypatch)
    facts = await accept.qualify_mask_references(masks.db, masks.value, probe())
    assert facts == masks.facts
    assert masks.events == [
        "session",
        "owner",
        "rollback",
        "physical-load-close",
        "owner",
        "rollback",
        "session-close",
    ]
    masks.facts[0]["receipt"]["id"] = "changed"
    assert facts[0]["receipt"]["id"] != "changed"


async def test_mask_preflight_and_open_business_transaction_refuse_before_provider(
    monkeypatch,
):
    masks = install_mask_flow(monkeypatch)
    masks.value[0]["geometry"]["keyframes"][0]["mask"]["size"] = [128, 96]
    with pytest.raises(ValueError):
        await accept.qualify_mask_references(masks.db, masks.value, probe())
    raster_mask_objects.load.assert_not_awaited()
    masks.db.in_transaction = lambda: True
    with pytest.raises(ContentError, match="io_transaction_open"):
        await accept.qualify_mask_references(masks.db, masks.value, probe())


async def test_actual_rle_mismatch_and_owner_switch_do_not_issue_facts(monkeypatch):
    masks = install_mask_flow(monkeypatch)
    monkeypatch.setattr(
        raster_mask_objects,
        "load",
        AsyncMock(return_value={**masks.rle, "counts": [128 * 96]}),
    )
    with pytest.raises(ValueError):
        await accept.qualify_mask_references(masks.db, masks.value, probe())
    masks = install_mask_flow(monkeypatch)
    changed = deepcopy(masks.facts[0])
    changed["gate_revision"] += 1
    monkeypatch.setattr(
        accept, "_mask_owner", AsyncMock(side_effect=[masks.facts[0], changed])
    )
    with pytest.raises(ContentError, match="mask_changed"):
        await accept.qualify_mask_references(masks.db, masks.value, probe())


async def test_final_mask_locks_use_exact_fact_set_and_never_read_content(monkeypatch):
    masks = install_mask_flow(monkeypatch)
    advisory = AsyncMock()
    monkeypatch.setattr(raster_mask_storage, "lock_raster_mask_references", advisory)
    monkeypatch.setattr(
        accept, "_mask_session", lambda db: pytest.fail("final must not open sessions")
    )
    await accept.lock_mask_references(masks.db, masks.value, probe(), masks.facts)
    advisory.assert_awaited_once_with(masks.db, masks.value, verify=False)
    raster_mask_objects.load.assert_not_awaited()
    with pytest.raises(ContentError, match="mask_changed"):
        await accept.lock_mask_references(masks.db, masks.value, probe(), [])
    changed = deepcopy(masks.facts)
    changed[0]["gate_revision"] += 1
    with pytest.raises(ContentError, match="mask_changed"):
        await accept.lock_mask_references(masks.db, masks.value, probe(), changed)


def test_saved_generation_mask_owner_is_not_replaceable_with_same_logical_sha():
    current = world()
    _, _, _, facts = owned_mask()
    current.evidence["result_qualification"]["masks"] = deepcopy(facts)
    accept._generation_masks(current.evidence, facts)
    changed = deepcopy(facts)
    changed[0]["receipt"]["id"] = str(uuid.uuid4())
    with pytest.raises(ContentError, match="mask_changed"):
        accept._generation_masks(current.evidence, changed)


async def test_sql_nowait_maps_to_stable_acceptance_busy_without_provider(monkeypatch):
    current = world()
    install_flow(monkeypatch, current)
    receipt = await accept.qualify_acceptance(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    failure = DBAPIError("SELECT", {}, SimpleNamespace(sqlstate="55P03"))
    monkeypatch.setattr(accept, "_current", AsyncMock(side_effect=failure))
    with pytest.raises(ContentError, match="acceptance_busy"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(),
        )


async def test_offline_rejection_has_current_write_auth_but_cannot_be_used_to_accept(
    monkeypatch,
):
    current = world()
    current.metadata.extra.clear()
    monkeypatch.setattr(
        common, "capture_credentials", AsyncMock(return_value=current.credentials)
    )
    monkeypatch.setattr(accept, "lock_actor_project_authority", AsyncMock())
    monkeypatch.setattr(accept, "lock_actor_scope", AsyncMock())
    credentials = AsyncMock(return_value=current.actor)
    monkeypatch.setattr(accept, "_credentials", credentials)
    monkeypatch.setattr(accept, "lock_task_scope", AsyncMock())
    monkeypatch.setattr(
        accept,
        "TaskLockService",
        lambda db: SimpleNamespace(_lock_task_scope=AsyncMock()),
    )
    monkeypatch.setattr(accept, "_authorize", AsyncMock())
    monkeypatch.setattr(
        accept,
        "_candidate",
        AsyncMock(return_value=(current.prediction, current.metadata)),
    )
    monkeypatch.setattr(
        originals, "open_snapshot", AsyncMock(side_effect=AssertionError("offline"))
    )
    monkeypatch.setattr(
        originals,
        "managed_binding",
        AsyncMock(side_effect=AssertionError("no owner needed")),
    )
    current.db.scalar = AsyncMock(return_value=current.task)
    receipt = await accept.qualify_rejection(
        current.db, current.task, current.actor, current.request, current.prediction.id
    )
    await accept.recheck_credentials(current.db, receipt)
    assert credentials.await_count == 2
    originals.open_snapshot.assert_not_awaited()
    originals.managed_binding.assert_not_awaited()
    with pytest.raises(ContentError, match="receipt_invalid"):
        await accept.validate_acceptance(
            current.db,
            current.prediction,
            current.actor.id,
            receipt,
            selection=accept.selection_value(),
        )


async def test_final_segment_lock_refreshes_full_cached_assignee_and_lease(monkeypatch):
    from app.services import video_collaboration

    current = world()
    segment_id = uuid.uuid4()
    selection = accept.selection_value(0, video_segment_id=segment_id)
    refreshed = []

    async def scalar(statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert (
            "video_segments.assignee_id" in sql
            and "video_segments.lock_expires_at" in sql
        )
        assert statement.get_execution_options()["populate_existing"] is True
        assert "FOR SHARE NOWAIT" in sql
        refreshed.append(True)
        return SimpleNamespace(id=segment_id)

    async def scope(*args, **kwargs):
        assert refreshed

    current.db.scalar, current.db.execute = scalar, AsyncMock()
    monkeypatch.setattr(
        video_collaboration, "assert_video_annotation_write_scope", scope
    )
    await accept._segment(
        current.db,
        current.task,
        current.actor,
        selection,
        current.prediction,
        [0],
        lock=True,
    )
    assert refreshed == [True]
