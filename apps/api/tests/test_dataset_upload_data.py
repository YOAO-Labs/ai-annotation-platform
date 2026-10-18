from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.dataset_upload_contract import (
    DatasetUploadAcceptance,
    DatasetUploadCompletion,
    DatasetUploadDiscard,
    DatasetUploadDuplicate,
    DatasetUploadFailure,
    DatasetUploadIntent,
    DatasetUploadReserve,
    decode_dataset_upload_reply,
)
from app.services.dataset_upload_data import DatasetUploadData

ACTOR_ID = uuid.uuid4()
DATASET_ID = uuid.uuid4()
ITEM_ID = uuid.uuid4()
PUBLICATION_ID = uuid.uuid4()
DUPLICATE_ID = uuid.uuid4()
CONTENT_HASH = "a" * 32
FILE_NAME = "sensors/front/frame.jpg"
CONTENT_TYPE = "image/jpeg"
STAGING_KEY = f"dataset-uploads/{ITEM_ID}/source"
FILE_PATH = f"dataset-sources/{DATASET_ID}/{ITEM_ID}/{PUBLICATION_ID}/{FILE_NAME}"


def _facts(
    *,
    state: str = "pending",
    publication_id: uuid.UUID | None = None,
    file_path: str | None = None,
    result: dict | None = None,
) -> dict:
    return {
        "item_id": str(ITEM_ID),
        "actor_id": str(ACTOR_ID),
        "dataset_id": str(DATASET_ID),
        "file_name": FILE_NAME,
        "file_path": file_path,
        "file_type": "image",
        "content_type": CONTENT_TYPE,
        "staging_key": STAGING_KEY,
        "publication_id": str(publication_id) if publication_id else None,
        "state": state,
        "result": result,
    }


def _success(data: dict) -> dict:
    return {"schema": "aap-dataset-upload/v1", "ok": True, "data": data}


class _FakeDb:
    def __init__(self, *payloads: object):
        self.payloads = list(payloads)
        self.events: list[tuple[str, object]] = []
        self.commits = 0
        self.flushes = 0

    async def scalar(self, statement, params: dict) -> object:
        self.events.append((str(statement), params))
        return self.payloads.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1


def test_frozen_schema_matches_the_typed_value_bindings():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/sql/0207_dataset_upload_intents.schema.json"
    )
    schema = json.loads(path.read_text())
    definitions = schema["$defs"]

    def check_refs(value: object) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                assert ref.removeprefix("#/$defs/") in definitions
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)
    models = (
        DatasetUploadReserve,
        DatasetUploadCompletion,
        DatasetUploadDiscard,
        DatasetUploadIntent,
        DatasetUploadAcceptance,
        DatasetUploadDuplicate,
    )
    for model in models:
        expected = model.model_json_schema()
        expected.pop("$defs", None)
        assert definitions[model.__name__] == expected

    assert definitions["DatasetUploadCompletedResult"]
    assert definitions["DatasetUploadDiscardedResult"]
    assert definitions["failure"]["properties"]["error"]["properties"]["code"][
        "enum"
    ] == [
        "invalid_request",
        "account_not_active",
        "account_busy",
        "invalid_account_role",
        "dataset_missing",
        "upload_missing",
        "upload_scope_mismatch",
        "upload_not_pending",
        "dataset_busy",
        "operation_conflict",
        "source_changed",
    ]


def test_intent_values_reject_unsafe_paths_and_inconsistent_states():
    reserve = {
        "item_id": ITEM_ID,
        "actor_id": ACTOR_ID,
        "dataset_id": DATASET_ID,
        "file_name": FILE_NAME,
        "content_type": CONTENT_TYPE,
    }
    assert DatasetUploadReserve(**reserve).file_name == FILE_NAME
    for unsafe in (
        "/absolute.jpg",
        "C:/absolute.jpg",
        "../escape.jpg",
        "a/../escape.jpg",
        "a//frame.jpg",
        "a/./frame.jpg",
        "a\\frame.jpg",
        "a\nframe.jpg",
    ):
        with pytest.raises(ValidationError):
            DatasetUploadReserve(**(reserve | {"file_name": unsafe}))
    with pytest.raises(ValidationError):
        DatasetUploadReserve(**(reserve | {"content_type": "image/jpeg\n"}))

    with pytest.raises(ValidationError):
        DatasetUploadCompletion(
            actor_id=ACTOR_ID,
            dataset_id=DATASET_ID,
            item_id=ITEM_ID,
            publication_id=PUBLICATION_ID,
            file_size=-1,
            content_hash=CONTENT_HASH,
            width=1,
            height=2,
        )
    with pytest.raises(ValidationError):
        DatasetUploadCompletion(
            actor_id=ACTOR_ID,
            dataset_id=DATASET_ID,
            item_id=ITEM_ID,
            publication_id=PUBLICATION_ID,
            file_size=1,
            content_hash="bad",
            width=1,
            height=2,
        )

    with pytest.raises(ValidationError):
        DatasetUploadIntent.model_validate(
            _facts(publication_id=PUBLICATION_ID, file_path=None)
        )
    with pytest.raises(ValidationError):
        DatasetUploadIntent.model_validate(
            _facts(
                state="completed", publication_id=PUBLICATION_ID, file_path=FILE_PATH
            )
        )

    duplicate = DatasetUploadAcceptance.model_validate(
        _facts(
            state="discarded",
            publication_id=PUBLICATION_ID,
            file_path=FILE_PATH,
            result={"duplicate_of": str(DUPLICATE_ID)},
        )
        | {"disposition": "duplicate", "duplicate_of": str(DUPLICATE_ID)}
    )
    assert duplicate.disposition == "duplicate"
    assert duplicate.duplicate_of == DUPLICATE_ID


@pytest.mark.asyncio
async def test_data_binding_preserves_scope_publication_and_caller_transaction():
    reserve_request = DatasetUploadReserve(
        item_id=ITEM_ID,
        actor_id=ACTOR_ID,
        dataset_id=DATASET_ID,
        file_name=FILE_NAME,
        content_type=CONTENT_TYPE,
    )
    completion = DatasetUploadCompletion(
        actor_id=ACTOR_ID,
        dataset_id=DATASET_ID,
        item_id=ITEM_ID,
        publication_id=PUBLICATION_ID,
        file_size=1234,
        content_hash=CONTENT_HASH,
        width=640,
        height=480,
    )
    discarded_facts = _facts(state="discarded", result={"reason": "missing"})
    db = _FakeDb(
        _success(_facts()),
        _success(
            _facts(
                publication_id=PUBLICATION_ID,
                file_path=FILE_PATH,
            )
        ),
        _success({"duplicate_of": str(DUPLICATE_ID)}),
        _success(
            _facts(
                state="completed",
                publication_id=PUBLICATION_ID,
                file_path=FILE_PATH,
                result={"item_id": str(ITEM_ID), "linked_tasks": 3},
            )
            | {"disposition": "accepted", "duplicate_of": None}
        ),
        _success(discarded_facts),
    )
    data = DatasetUploadData(db)

    reserved = await data.reserve(reserve_request)
    locked = await data.lock(actor_id=ACTOR_ID, dataset_id=DATASET_ID, item_id=ITEM_ID)
    duplicate = await data.find_duplicate(
        actor_id=ACTOR_ID,
        dataset_id=DATASET_ID,
        item_id=ITEM_ID,
        content_hash=CONTENT_HASH.upper(),
    )
    completed = await data.complete(completion)
    discarded = await data.discard(
        DatasetUploadDiscard(
            actor_id=ACTOR_ID,
            dataset_id=DATASET_ID,
            item_id=ITEM_ID,
            reason="missing",
        )
    )

    assert isinstance(reserved, DatasetUploadIntent)
    assert reserved.publication_id is None and reserved.file_path is None
    assert locked.publication_id == PUBLICATION_ID
    assert isinstance(duplicate, DatasetUploadDuplicate)
    assert duplicate.duplicate_of == DUPLICATE_ID
    assert isinstance(completed, DatasetUploadAcceptance)
    assert completed.disposition == "accepted"
    assert completed.result.item_id == ITEM_ID
    assert completed.result.linked_tasks == 3
    assert discarded.state == "discarded"
    assert discarded.result.reason == "missing"

    statements = [statement for statement, _ in db.events]
    assert "reserve_dataset_upload" in statements[0]
    assert "lock_dataset_upload" in statements[1]
    assert "find_dataset_upload_duplicate" in statements[2]
    assert "complete_dataset_upload" in statements[3]
    assert "discard_dataset_upload" in statements[4]
    assert db.events[2][1]["content_hash"] == CONTENT_HASH
    assert db.events[3][1]["publication"] == PUBLICATION_ID
    assert db.commits == 0
    assert db.flushes == 0


@pytest.mark.asyncio
async def test_data_binding_rejects_wrong_scope_and_preserves_owner_failures():
    wrong_scope = _facts() | {"actor_id": str(uuid.uuid4())}
    db = _FakeDb(_success(wrong_scope))
    with pytest.raises(ValueError, match="does not match its request scope"):
        await DatasetUploadData(db).reserve(
            DatasetUploadReserve(
                item_id=ITEM_ID,
                actor_id=ACTOR_ID,
                dataset_id=DATASET_ID,
                file_name=FILE_NAME,
                content_type=CONTENT_TYPE,
            )
        )
    assert db.commits == 0

    with pytest.raises(DatasetUploadFailure, match="source_changed"):
        decode_dataset_upload_reply(
            {
                "schema": "aap-dataset-upload/v1",
                "ok": False,
                "error": {"code": "source_changed"},
            },
            DatasetUploadIntent,
        )
    with pytest.raises(ValidationError):
        decode_dataset_upload_reply(
            {
                "schema": "aap-dataset-upload/v1",
                "ok": True,
                "data": _facts() | {"internal": True},
            },
            DatasetUploadIntent,
        )
