from importlib.resources import files
import json
from uuid import UUID

from pydantic import ValidationError
import pytest

from aap_assets_source.import_contract import (
    ImportAcceptance,
    ImportConnection,
    ImportContext,
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportFailureCode,
    ImportLimits,
    ImportSourceObject,
    ImportState,
    decode_import_reply,
)

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
DATASET = UUID("20000000-0000-4000-8000-000000000002")
CONNECTION = UUID("30000000-0000-4000-8000-000000000003")
JOB = UUID("40000000-0000-4000-8000-000000000004")
ITEM = UUID("50000000-0000-4000-8000-000000000005")
PUBLICATION = UUID("60000000-0000-4000-8000-000000000006")
LEASE = UUID("70000000-0000-4000-8000-000000000007")


def _limits() -> dict:
    return {
        "dataset_import_max_files": 100,
        "dataset_import_max_total_bytes": 1_000_000,
        "version": "v1-test",
    }


def _state(**updates) -> dict:
    value = {
        "job_id": JOB,
        "actor_id": ACTOR,
        "dataset_id": DATASET,
        "connection_id": CONNECTION,
        "source_path": "batch-a",
        "recursive": True,
        "include_globs": ["*.jpg"],
        "settings_snapshot": _limits(),
        "stage": "enumerating",
        "cursor": 0,
        "total": None,
        "total_bytes": None,
        "added": 0,
        "skipped": 0,
        "error_count": 0,
        "errors": [],
        "linked_tasks": 0,
        "committed_bytes": 0,
        "lease_token": LEASE,
        "lease_expires_at": "2026-10-18T10:00:00Z",
    }
    value.update(updates)
    return value


def test_import_values_are_bounded_and_retain_the_existing_dataset_display_id():
    context = ImportContext(
        actor_id=ACTOR,
        dataset_id=DATASET,
        dataset_display_id="DS-ABC123",
        dataset_name="training",
        connection_id=CONNECTION,
        connection_name="source",
        connection_kind="s3",
        connection_config={"endpoint": "https://storage.example", "bucket": "data"},
    )
    empty_name = ImportContext(
        actor_id=ACTOR,
        dataset_id=DATASET,
        dataset_display_id="DS-ABC123",
        dataset_name="",
        connection_id=CONNECTION,
        connection_name="source",
        connection_kind="s3",
        connection_config={"endpoint": "https://storage.example", "bucket": "data"},
    )
    acceptance = ImportAcceptance(
        job_id=JOB,
        actor_id=ACTOR,
        dataset_id=DATASET,
        connection_id=CONNECTION,
    )
    assert context.dataset_display_id == "DS-ABC123"
    assert empty_name.dataset_name == ""
    assert acceptance.job_id == JOB
    assert ImportLimits.model_validate(_limits()).dataset_import_max_files == 100
    connection = ImportConnection(
        job_id=JOB,
        lease_token=LEASE,
        kind="s3",
        config={"endpoint": "https://storage.example", "bucket": "data"},
        secret_enc="ZW5jcnlwdGVk",
        allowlist=["storage.example"],
    )
    assert connection.secret_enc == "ZW5jcnlwdGVk"

    with pytest.raises(ValidationError):
        ImportContext(
            actor_id=ACTOR,
            dataset_id=DATASET,
            dataset_display_id="DS-ABC123",
            dataset_name="training",
            connection_id=CONNECTION,
            connection_name="source",
            connection_kind="s3",
            connection_config={"access_key": "secret"},
        )
    with pytest.raises(ValidationError):
        ImportLimits(
            dataset_import_max_files=True,
            dataset_import_max_total_bytes=1,
            version="v1",
        )
    with pytest.raises(ValidationError):
        ImportConnection(
            job_id=JOB,
            lease_token=LEASE,
            kind="s3",
            config={"endpoint": "https://storage.example", "bucket": "data"},
            secret_enc="not base64!",
            allowlist=["storage.example"],
        )


def test_source_objects_reject_traversal_and_require_timezone_aware_mtime():
    assert (
        ImportSourceObject(
            relpath="scene/image.jpg", size=12, mtime=None, etag=None
        ).size
        == 12
    )
    for relpath in ("../escape.jpg", "/absolute.jpg", "folder\\escape.jpg"):
        with pytest.raises(ValidationError):
            ImportSourceObject(relpath=relpath, size=1, mtime=None, etag=None)
    with pytest.raises(ValidationError):
        ImportSourceObject(
            relpath="image.jpg",
            size=1,
            mtime="2026-10-18T10:00:00",
            etag=None,
        )
    assert ImportSourceObject(
        relpath=f"prefix/{'a' * 1000}", size=1, mtime=None, etag=None
    ).relpath.startswith("prefix/")


def test_import_state_requires_a_frozen_manifest_before_copying():
    state = ImportState.model_validate(
        _state(
            stage="importing",
            total=2,
            total_bytes=20,
            cursor=1,
            added=1,
        )
    )
    assert state.cursor == 1
    for updates in (
        {"stage": "importing"},
        {"stage": "importing", "total": 1, "total_bytes": 1, "cursor": 2},
        {
            "stage": "cancelled",
            "lease_token": None,
            "lease_expires_at": None,
            "errors": ["x"] * 101,
        },
        {"stage": "importing", "total": 2, "total_bytes": 20, "cursor": 1},
        {
            "stage": "completed",
            "total": 2,
            "total_bytes": 20,
            "cursor": 1,
            "added": 1,
            "lease_token": None,
            "lease_expires_at": None,
        },
        {
            "committed_bytes": 1_000_001,
        },
    ):
        with pytest.raises(ValidationError):
            ImportState.model_validate(_state(**updates))


def test_import_entry_freezes_safe_publication_path_and_completion_result():
    entry = ImportEntry(
        job_id=JOB,
        lease_token=LEASE,
        index=0,
        item_id=ITEM,
        publication_id=PUBLICATION,
        file_name="scene/image.jpg",
        file_path=f"dataset-sources/{DATASET}/{ITEM}/{PUBLICATION}/scene/image.jpg",
        file_type="image",
        source=ImportSourceObject(
            relpath="source/scene/image.jpg", size=25, mtime=None, etag="etag"
        ),
    )
    assert entry.file_type == "image"
    assert (
        ImportEntryCompletion(
            status="copied", file_size=25, content_hash="a" * 32
        ).file_size
        == 25
    )
    assert (
        ImportEntryCompletion(status="error", error_code="source_changed").status
        == "error"
    )

    with pytest.raises(ValidationError):
        ImportEntry(
            job_id=JOB,
            lease_token=LEASE,
            index=0,
            item_id=ITEM,
            publication_id=PUBLICATION,
            file_name="scene/image.jpg",
            file_path="dataset-sources/other/image.jpg",
            file_type="image",
            source=ImportSourceObject(
                relpath="source/scene/image.jpg", size=25, mtime=None, etag="etag"
            ),
        )
    for completion in (
        {"status": "copied", "file_size": 25},
        {"status": "copied", "file_size": 25, "content_hash": "not-md5"},
        {"status": "copied", "file_size": 25, "content_hash": "a" * 32, "width": 10},
        {"status": "error", "error_code": "Network timeout with credential"},
    ):
        with pytest.raises(ValidationError):
            ImportEntryCompletion.model_validate(completion)


def test_import_reply_decoder_rejects_malformed_and_unknown_errors():
    success = decode_import_reply(
        {
            "schema": "aap-dataset-import/v1",
            "ok": True,
            "data": {
                "job_id": str(JOB),
                "actor_id": str(ACTOR),
                "dataset_id": str(DATASET),
                "connection_id": str(CONNECTION),
            },
        },
        ImportAcceptance,
    )
    assert success is not None and success.job_id == JOB
    assert (
        decode_import_reply(
            {"schema": "aap-dataset-import/v1", "ok": True, "data": None},
            ImportState,
        )
        is None
    )

    with pytest.raises(ImportFailure) as failure:
        decode_import_reply(
            {
                "schema": "aap-dataset-import/v1",
                "ok": False,
                "error": {"code": "unknown"},
            },
            ImportState,
        )
    assert failure.value.code == "invalid_response"
    with pytest.raises(ImportFailure) as failure:
        decode_import_reply(
            {
                "schema": "aap-dataset-import/v1",
                "ok": False,
                "error": {"code": "connection_changed"},
            },
            ImportState,
        )
    assert failure.value.code == "connection_changed"
    with pytest.raises(ImportFailure) as failure:
        decode_import_reply(
            {"schema": "aap-dataset-import/v2", "ok": True, "data": {}},
            ImportState,
        )
    assert failure.value.code == "invalid_response"


def test_packaged_schema_tracks_public_import_models_and_failure_codes():
    schema = json.loads(
        files("aap_assets_source")
        .joinpath("import_contract.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["$id"] == "urn:aap:dataset-import:v1"
    definitions = schema["$defs"]
    for model in (
        ImportAcceptance,
        ImportConnection,
        ImportContext,
        ImportEntry,
        ImportEntryCompletion,
        ImportLimits,
        ImportSourceObject,
        ImportState,
    ):
        expected = model.model_json_schema(ref_template="#/$defs/{model}")
        nested = expected.pop("$defs", {})
        assert definitions[model.__name__] == expected
        for name, value in nested.items():
            assert definitions[name] == value
    assert definitions["ImportFailureCode"]["enum"] == list(ImportFailureCode.__args__)
    assert "reply" in definitions
