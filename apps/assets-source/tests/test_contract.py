from uuid import UUID

import pytest
from pydantic import ValidationError

from aap_assets_source import (
    DatasetUploadCompletedResult,
    DatasetUploadFailure,
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
    LegacyUploadState,
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
    decode_legacy_upload_reply,
    decode_source_reply,
    validate_relative_file_name,
)

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
DATASET = UUID("20000000-0000-4000-8000-000000000002")
ITEM = UUID("30000000-0000-4000-8000-000000000003")
OPERATION = UUID("40000000-0000-4000-8000-000000000004")
OTHER_ITEM = UUID("50000000-0000-4000-8000-000000000005")


def _legacy_state(**overrides) -> dict:
    value = {
        "operation_id": str(OPERATION),
        "actor_id": str(ACTOR),
        "dataset_id": str(DATASET),
        "item_id": str(ITEM),
        "file_path": "datasets/example.jpg",
        "file_type": "image",
        "width": 640,
        "height": 480,
        "state": "pending",
        "result": None,
        "duplicate_of": None,
    }
    value.update(overrides)
    return value


def test_public_upload_requests_and_init_result_validate_values() -> None:
    request = UploadInitRequest(
        item_id=ITEM,
        actor_id=ACTOR,
        dataset_id=DATASET,
        file_name="folder/image.png",
        content_type="image/png",
    )
    assert request.file_name == "folder/image.png"
    assert request.content_type == "image/png"
    assert validate_relative_file_name("folder/image.png") == "folder/image.png"

    result = UploadInitResult(
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        upload_url="https://storage.example/upload?signature=abc",
    )
    assert result.expires_in == 900
    relative = UploadInitResult(
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        upload_url="/minio/bucket/item?signature=abc",
    )
    assert relative.upload_url == "/minio/bucket/item?signature=abc"
    assert (
        UploadCompleteRequest(
            operation_id=OPERATION,
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
        ).item_id
        == ITEM
    )


@pytest.mark.parametrize(
    "upload_url",
    [
        "file:///tmp/source",
        "https:///missing-host",
        "https://user:pass@storage.example/object",
        "https://storage.example/object\nX-Injected: true",
        "https://storage.example/object\x85X-Injected: true",
        "//storage.example/object",
        "/\\storage.example/object",
    ],
)
def test_upload_init_result_rejects_unsafe_url(upload_url: str) -> None:
    with pytest.raises(ValidationError):
        UploadInitResult(
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
            upload_url=upload_url,
        )


def test_upload_outcome_enforces_accepted_duplicate_and_missing_branches() -> None:
    missing = UploadOutcome(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        file_type="image",
        result=None,
        duplicate_of=None,
    )
    accepted = UploadOutcome(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        file_type="legacy/custom",
        result=DatasetUploadCompletedResult(item_id=ITEM, linked_tasks=2),
        duplicate_of=None,
    )
    duplicate = UploadOutcome(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        file_type="image",
        result=None,
        duplicate_of=OTHER_ITEM,
    )

    assert missing.result is None and missing.duplicate_of is None
    assert accepted.result is not None and accepted.result.item_id == ITEM
    assert duplicate.duplicate_of == OTHER_ITEM

    with pytest.raises(ValidationError):
        UploadOutcome(
            operation_id=OPERATION,
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
            file_type="image",
            result=DatasetUploadCompletedResult(item_id=ITEM, linked_tasks=0),
            duplicate_of=OTHER_ITEM,
        )
    with pytest.raises(ValidationError):
        UploadOutcome(
            operation_id=OPERATION,
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
            file_type="image",
            result=DatasetUploadCompletedResult(item_id=OTHER_ITEM, linked_tasks=0),
            duplicate_of=None,
        )


def test_source_reply_decoder_accepts_only_exact_typed_success_and_known_failure() -> (
    None
):
    result = decode_source_reply(
        {
            "schema": "aap-assets-source/v1",
            "ok": True,
            "data": {
                "actor_id": str(ACTOR),
                "dataset_id": str(DATASET),
                "item_id": str(ITEM),
                "upload_url": "http://localhost:9000/bucket/item?sig=x",
                "expires_in": 17,
            },
        },
        UploadInitResult,
    )
    assert isinstance(result, UploadInitResult)
    assert result.expires_in == 17

    with pytest.raises(SourceFailure) as failure:
        decode_source_reply(
            {
                "schema": "aap-assets-source/v1",
                "ok": False,
                "error": {"code": "storage_unavailable"},
            },
            UploadInitResult,
        )
    assert failure.value.code == "storage_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "aap-assets-source/v2", "ok": True, "data": {}},
        {
            "schema": "aap-assets-source/v1",
            "ok": True,
            "data": {},
            "error": None,
        },
        {
            "schema": "aap-assets-source/v1",
            "ok": False,
            "error": {"code": "unknown"},
        },
        {
            "schema": "aap-assets-source/v1",
            "ok": True,
            "data": {
                "actor_id": str(ACTOR),
                "dataset_id": str(DATASET),
                "item_id": str(ITEM),
                "upload_url": "ftp://storage.example/file",
            },
        },
    ],
)
def test_source_reply_decoder_rejects_invalid_envelopes_and_payloads(
    payload: dict,
) -> None:
    with pytest.raises(SourceFailure) as failure:
        decode_source_reply(payload, UploadInitResult)
    assert failure.value.code == "invalid_response"


def test_source_deletion_wire_request_excludes_bucket_configuration() -> None:
    request = SourceDeletionRequest(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
    )
    assert request.item_id == ITEM
    with pytest.raises(ValidationError):
        SourceDeletionRequest(
            operation_id=OPERATION,
            actor_id=ACTOR,
            dataset_id=DATASET,
            cleanup_bucket="source-bucket",
        )


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        ("pending", {}),
        (
            "completed",
            {
                "result": {"item_id": str(ITEM), "linked_tasks": 1},
            },
        ),
        ("duplicate", {"duplicate_of": str(OTHER_ITEM)}),
    ],
)
def test_legacy_upload_state_accepts_exact_dispositions(
    state: str, extra: dict
) -> None:
    result = LegacyUploadState.model_validate(_legacy_state(state=state, **extra))
    assert result.state == state


def test_legacy_lock_request_and_independent_existing_dimensions_are_supported() -> (
    None
):
    request = LegacyUploadLockRequest(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
    )
    state = LegacyUploadState.model_validate(_legacy_state(width=640, height=None))
    assert request.item_id == ITEM
    assert state.width == 640 and state.height is None


@pytest.mark.parametrize(
    "payload",
    [
        _legacy_state(
            state="pending", result={"item_id": str(ITEM), "linked_tasks": 0}
        ),
        _legacy_state(state="completed"),
        _legacy_state(
            state="completed", result={"item_id": str(OTHER_ITEM), "linked_tasks": 0}
        ),
        _legacy_state(state="duplicate", duplicate_of=str(ITEM)),
    ],
)
def test_legacy_upload_state_rejects_mixed_scope_or_terminal_branches(
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        LegacyUploadState.model_validate(payload)


def test_legacy_completion_canonicalizes_hash_and_checks_dimensions() -> None:
    completion = LegacyUploadCompletion(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        expected_file_path="legacy/source.jpg",
        file_size=123,
        content_hash="A" * 64,
        width=640,
        height=480,
    )
    assert completion.content_hash == "a" * 64
    with pytest.raises(ValidationError):
        LegacyUploadCompletion(
            operation_id=OPERATION,
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
            expected_file_path="legacy/source.jpg",
            file_size=123,
            content_hash=None,
            width=640,
            height=None,
        )


def test_legacy_upload_reply_decoder_checks_envelope_and_surfaces_known_failure() -> (
    None
):
    state = decode_legacy_upload_reply(
        {
            "schema": "aap-legacy-dataset-upload/v1",
            "ok": True,
            "data": _legacy_state(),
        }
    )
    assert state.state == "pending"

    with pytest.raises(DatasetUploadFailure) as failure:
        decode_legacy_upload_reply(
            {
                "schema": "aap-legacy-dataset-upload/v1",
                "ok": False,
                "error": {"code": "dataset_busy"},
            }
        )
    assert failure.value.code == "dataset_busy"


def test_public_source_packages_have_no_runtime_framework_imports() -> None:
    from pathlib import Path
    import ast

    source_root = Path(__file__).resolve().parents[1] / "src" / "aap_assets_source"
    prohibited = {"app", "fastapi", "sqlalchemy", "starlette"}
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", maxsplit=1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", maxsplit=1)[0]]
            else:
                continue
            assert prohibited.isdisjoint(names), path
