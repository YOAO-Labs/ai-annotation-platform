"""Private native receipts and source-bound Mask sessions (no physical mocks here)."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import uuid

from aap_protocol_v2 import encode_low_res_mask
from fastapi import HTTPException
import numpy as np
import pytest

from app.db.models.task import Task
from app.services import managed_native_inference as native
from app.services import prediction_originals as originals
from app.services.ai_mask_session import issue_ai_mask_session
from app.services.content_snapshot import ContentError
from app.services.ml_interaction_proxy import prepare_interactive_context
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    build_rle_reference,
    validate_image_mask_dimensions,
    validate_mask_geometry_for_task,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import reference_key

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 3]}


def identity(task_id):
    return {
        "schema": 1,
        "task_id": str(task_id),
        "owner_kind": "task_original",
        "content_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "size_bytes": 100,
        "width": 3,
        "height": 2,
        "location_id": str(uuid.uuid4()),
        "location_revision": 1,
        "maintenance_revision": 1,
        "facts_digest": "b" * 64,
    }


def capabilities():
    return {
        "models": [
            {
                "id": "sam-image",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["point"],
                "supported_inputs": ["full_image", "point_prompt"],
                "supported_geometric_outputs": ["mask"],
            }
        ]
    }


def prompt():
    return {"type": "point", "points": [[0.5, 0.5]], "output_geometry": "mask"}


def test_managed_source_identity_participates_in_prompt_revision():
    task_id = uuid.uuid4()
    source = identity(task_id)
    first, _ = prepare_interactive_context(
        prompt(), capabilities(), task_id=str(task_id), original_input=source
    )
    repeated, _ = prepare_interactive_context(
        prompt(), capabilities(), task_id=str(task_id), original_input=deepcopy(source)
    )
    changed, _ = prepare_interactive_context(
        prompt(),
        capabilities(),
        task_id=str(task_id),
        original_input={**source, "maintenance_revision": 2},
    )
    assert first["prompt_revision"] == repeated["prompt_revision"]
    assert first["prompt_revision"] != changed["prompt_revision"]


@pytest.mark.parametrize("mutation", ["missing", "source", "reference", "boolean"])
def test_managed_logits_session_cannot_lose_or_change_its_source(mutation):
    task_id = uuid.uuid4()
    source = identity(task_id)
    claims = {
        "task_id": str(task_id),
        "frame_index": None,
        "requested_backend_id": None,
        "model_id": "sam-image",
        "source": None,
        "model_variants": {},
        "original_input": source,
    }
    if mutation == "missing":
        claims.pop("original_input")
    elif mutation == "source":
        claims["original_input"] = {**source, "sha256": "c" * 64}
    elif mutation == "boolean":
        claims["original_input"] = {**source, "schema": True}
    else:
        claims["reference_input"] = {"schema": 1}
    token = issue_ai_mask_session(
        encode_low_res_mask(np.zeros((256, 256), dtype=np.float32)), claims
    )
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {**prompt(), "mask_input": token},
            capabilities(),
            task_id=str(task_id),
            original_input=source,
        )
    assert caught.value.detail["reason"] == "mask_session_mismatch"


def test_managed_logits_round_trip_and_mutually_exclusive_identity():
    task_id = uuid.uuid4()
    source = identity(task_id)
    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(
        raw,
        {
            "task_id": str(task_id),
            "frame_index": None,
            "requested_backend_id": None,
            "model_id": "sam-image",
            "source": None,
            "model_variants": {},
            "original_input": source,
        },
    )
    prepared, _ = prepare_interactive_context(
        {**prompt(), "mask_input": token},
        capabilities(),
        task_id=str(task_id),
        original_input=source,
    )
    assert prepared["mask_input"] == raw
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            prompt(),
            capabilities(),
            task_id=str(task_id),
            original_input=source,
            reference_input={"schema": 1},
        )
    assert caught.value.detail["reason"] == "mask_source_identity_conflict"


class Session:
    def __init__(self):
        self.transaction = object()
        self.nested = False

    def get_transaction(self):
        return self.transaction

    def in_nested_transaction(self):
        return self.nested


@pytest.fixture
def receipt_world(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        file_type="image",
        file_path="canonical/original.png",
        file_name="original.png",
        batch_id=None,
        dataset_item_id=None,
        version=4,
    )
    source = identity(task.id)
    actor_id = uuid.uuid4()
    # The unit boundary is the existing canonical resolver; live owned tests
    # separately register real TaskOriginal/direct/primary-only source facts.
    binding = {"source": "private canonical facts"}
    db = SimpleNamespace(sync_session=Session(), reads=[])

    async def get(model, identifier, **kwargs):
        assert model is Task
        db.reads.append(identifier)
        return task

    async def managed_binding(owned_db, current, *, lock=False):
        assert owned_db is db and current is task and lock is True
        return deepcopy(binding)

    db.get = get
    monkeypatch.setattr(originals, "managed_binding", managed_binding)
    mask = build_rle_reference(RLE)
    receipt = native._NativeReceipt(
        issuer=native._ISSUER,
        session=db.sync_session,
        transaction=db.sync_session.transaction,
        actor_id=actor_id,
        task_id=task.id,
        binding=originals.encoded({**task_binding(task), "original": binding}),
        idempotency_key="one-request",
        request_digest="digest",
        mask_digest=mask["sha256"],
        evidence=originals.encoded(source),
    )
    return SimpleNamespace(
        db=db,
        task=task,
        source=source,
        binding=binding,
        actor_id=actor_id,
        receipt=receipt,
        geometry={"type": "raster_mask", "mask": mask},
    )


@pytest.mark.parametrize(
    "mutation", ["plain", "issuer", "session", "transaction", "task", "actor", "nested"]
)
async def test_native_receipt_rejects_forgery_and_scope_changes_before_reads(
    receipt_world, mutation
):
    world = receipt_world
    receipt = world.receipt
    if mutation == "plain":
        receipt = dict(world.source)
    elif mutation == "nested":
        world.db.sync_session.nested = True
    else:
        field = {
            "issuer": "issuer",
            "session": "session",
            "transaction": "transaction",
            "task": "task_id",
            "actor": "actor_id",
        }[mutation]
        receipt = replace(receipt, **{field: object()})
    with pytest.raises(ContentError, match="prediction_original_receipt_invalid"):
        await native.validate_native_receipt(
            world.db,
            world.task.id,
            world.task.project_id,
            receipt,
            actor_id=world.actor_id,
        )
    assert world.db.reads == []


async def test_native_dimensions_work_without_direct_item_but_not_without_receipt(
    receipt_world,
):
    world = receipt_world
    await validate_mask_geometry_for_task(
        world.db,
        world.task,
        world.geometry,
        native_original_receipt=world.receipt,
    )
    with pytest.raises(RasterMaskContractError) as caught:
        await validate_mask_geometry_for_task(world.db, world.task, world.geometry)
    assert caught.value.detail["reason"] == "raster_mask_dataset_item_required"
    with pytest.raises(RasterMaskContractError) as caught:
        await validate_mask_geometry_for_task(
            world.db,
            world.task,
            {**world.geometry, "mask": {**world.geometry["mask"], "size": [3, 2]}},
            native_original_receipt=world.receipt,
        )
    assert caught.value.detail["reason"] == "raster_mask_size_mismatch"


@pytest.mark.parametrize("mutation", ["version", "source", "reference", "video"])
async def test_native_receipt_refuses_changed_current_binding(receipt_world, mutation):
    world = receipt_world
    code = "content_binding_changed"
    if mutation == "version":
        world.task.version += 1
    elif mutation == "source":
        world.binding["source"] = "a different canonical original"
    elif mutation == "video":
        world.task.file_type = "video"
        code = "prediction_original_receipt_invalid"
    else:
        world.task.file_path = reference_key(uuid.uuid4(), "a" * 64)
        code = "prediction_original_receipt_invalid"
    with pytest.raises(ContentError, match=code):
        await native.validate_native_receipt(
            world.db, world.task.id, world.task.project_id, world.receipt
        )


@pytest.mark.parametrize(
    "result",
    [
        [],
        [{"geometry": {"type": "polygon"}}],
        [{"geometry": {"type": "raster_mask", "mask": {"sha256": "0" * 64}}}],
    ],
)
async def test_native_receipt_is_bound_to_one_exact_mask(receipt_world, result):
    world = receipt_world
    with pytest.raises(
        ContentError, match="prediction_original_geometry_not_supported"
    ):
        await native.validate_native_receipt(
            world.db, world.task.id, world.task.project_id, world.receipt, result=result
        )


async def test_native_receipt_is_bound_to_request_and_idempotency_key(
    receipt_world, monkeypatch
):
    from app.services import ai_mask_accept

    world = receipt_world
    monkeypatch.setattr(
        ai_mask_accept, "ai_mask_accept_request_digest", lambda data: "digest"
    )
    for key in ("one-request", "different-request"):
        data = SimpleNamespace(idempotency_key=key)
        if key == "one-request":
            await native.validate_native_receipt(
                world.db, world.task.id, world.task.project_id, world.receipt, data=data
            )
        else:
            with pytest.raises(
                ContentError, match="prediction_original_receipt_invalid"
            ):
                await native.validate_native_receipt(
                    world.db,
                    world.task.id,
                    world.task.project_id,
                    world.receipt,
                    data=data,
                )


@pytest.mark.parametrize(
    "width,height,size,reason",
    [
        (None, 2, [2, 3], "raster_mask_source_dimensions_missing"),
        (8193, 2, [2, 8193], "raster_mask_dimensions_exceeded"),
        (3, 2, [3, 2], "raster_mask_size_mismatch"),
    ],
)
def test_pure_dimensions_matcher_preserves_limits(width, height, size, reason):
    with pytest.raises(RasterMaskContractError) as caught:
        validate_image_mask_dimensions({"mask": {"size": size}}, width, height)
    assert caught.value.detail["reason"] == reason
