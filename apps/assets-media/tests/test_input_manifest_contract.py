"""Portable manifest barriers, frozen scope and bounded cursor behavior."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from pydantic import ValidationError
import pytest

from aap_assets_input_manifests import contract as c


def scope():
    return {"job_id": uuid4(), "actor_id": uuid4(), "project_id": uuid4()}


def header(**changes):
    now = datetime.now(timezone.utc)
    return c.ManifestHeader.model_validate(
        {
            **scope(),
            "selection": {"predict_mode": "skip_predicted"},
            "selection_digest": "a" * 64,
            "candidate_count": 1503,
            "selected_count": 1503,
            "prepared_count": 0,
            "status": "preparing",
            "ready_count": None,
            "failed_count": None,
            "sealed_digest": None,
            "created_at": now,
            "sealed_at": None,
            **changes,
        }
    )


def source(manifest, task_id, *, identity=None):
    return {
        "owner": {"kind": "task", "id": task_id},
        "task_id": task_id,
        "project_id": manifest.project_id,
        "dataset_id": None,
        "item_id": None,
        "source_generation": 2,
        "source_snapshot": "b" * 64,
        "object_identity": identity,
        "source_drifted": False,
        "upload_ready": True,
        "file_path": "images/original.png",
        "file_name": "original.png",
        "file_type": "image",
        "bucket_kind": "legacy",
        "file_size": None,
        "content_hash": None,
        "width": 3,
        "height": 2,
        "thumbnail_path": None,
        "blurhash": None,
        "thumbnail_source_generation": None,
        "thumbnail_error": None,
    }


def member(manifest, ordinal, *, state="unprepared", **changes):
    task_id = uuid4()
    raw = {
        "ordinal": ordinal,
        "task_id": task_id,
        "project_id": manifest.project_id,
        "task_version": 4,
        "task_source_generation": 2,
        "dataset_item_id": None,
        "task_source_snapshot": "c" * 64,
        "file_path": "images/original.png",
        "file_type": "image",
        "source": source(manifest, task_id),
        "state": state,
        "input": None,
        "error": None,
    }
    if state in {"preparing", "ready"}:
        raw["input"] = {
            "input_id": uuid4(),
            "job_id": manifest.job_id,
            "actor_id": manifest.actor_id,
            "project_id": manifest.project_id,
            "task_id": task_id,
            "status": "ready" if state == "ready" else "pending",
            "task_version": 4,
            "task_source_generation": 2,
            "dataset_item_id": None,
            "task_source_snapshot": "c" * 64,
            "source_owner": {"kind": "task", "id": task_id},
            "source_generation": 2,
            "source_snapshot": "b" * 64,
            "byte_size": 200 if state == "ready" else None,
            "sha256": "d" * 64 if state == "ready" else None,
            "error": None,
            "created_at": datetime.now(timezone.utc),
            "retire_after": None,
        }
    if state == "failed":
        raw["error"] = "source_changed"
    return c.ManifestMember.model_validate({**raw, **changes})


def test_all_selection_has_no_total_limit_but_explicit_selector_keeps_api_limit():
    assert (
        header(candidate_count=10_000_000, selected_count=10_000_000).selection.task_ids
        is None
    )
    ids = [uuid4(), uuid4()]
    selection = c.ManifestSelection(predict_mode="append", task_ids=ids + ids)
    assert selection.task_ids == sorted(ids, key=str)
    with pytest.raises(ValidationError):
        c.ManifestSelection(
            predict_mode="append", task_ids=[uuid4() for _ in range(201)]
        )
    with pytest.raises(ValidationError):
        c.ManifestSelection(predict_mode="append", task_ids=[])


@pytest.mark.parametrize(
    "changes",
    [
        {"prepared_count": 1504},
        {"ready_count": 1},
        {"selected_count": True},
        {"selected_count": 1504},
        {"status": "sealed"},
        {
            "status": "sealed",
            "prepared_count": 1503,
            "ready_count": 1502,
            "failed_count": 0,
            "sealed_digest": "e" * 64,
            "sealed_at": datetime.now(timezone.utc),
        },
    ],
)
def test_header_cannot_claim_a_partial_or_unmeasured_seal(changes):
    with pytest.raises(ValidationError):
        header(**changes)


def test_page_progress_is_contiguous_and_bound_to_request_digest():
    manifest = header()
    page = c.ManifestPage(
        manifest=manifest,
        cursor=100,
        next_cursor=102,
        members=[member(manifest, 101), member(manifest, 102)],
    )
    request = c.ManifestPageRequest(
        **{
            name: getattr(manifest, name)
            for name in ("job_id", "actor_id", "project_id")
        },
        selection_digest=manifest.selection_digest,
        cursor=100,
        page_size=2,
    )
    c.require_scope(request, page)
    with pytest.raises(ValueError):
        c.require_scope(request.model_copy(update={"page_size": 1}), page)
    with pytest.raises(ValueError):
        c.require_scope(request.model_copy(update={"selection_digest": "f" * 64}), page)


@pytest.mark.parametrize(
    "fault", ["empty", "gap", "backward", "premature_end", "too_many"]
)
def test_pages_cannot_silently_omit_or_repeat_members(fault):
    manifest = header()
    raw = {
        "manifest": manifest,
        "cursor": 0,
        "next_cursor": 1,
        "members": [member(manifest, 1)],
    }
    if fault == "empty":
        raw["members"] = []
    elif fault == "gap":
        raw["members"] = [member(manifest, 2)]
        raw["next_cursor"] = 2
    elif fault == "backward":
        raw["next_cursor"] = 0
    elif fault == "premature_end":
        raw["next_cursor"] = None
    else:
        raw["members"] = [member(manifest, n) for n in range(1, 102)]
        raw["next_cursor"] = 101
    with pytest.raises(ValidationError):
        c.ManifestPage.model_validate(raw)


def test_sealed_digest_and_scope_reject_a_crossed_reply():
    now = datetime.now(timezone.utc)
    manifest = header(
        candidate_count=1,
        selected_count=1,
        prepared_count=1,
        status="sealed",
        ready_count=1,
        failed_count=0,
        sealed_digest="e" * 64,
        created_at=now,
        sealed_at=now,
    )
    page = c.ManifestPage(
        manifest=manifest,
        cursor=0,
        next_cursor=None,
        members=[member(manifest, 1, state="ready")],
    )
    request = c.ManifestPageRequest(
        job_id=manifest.job_id,
        actor_id=manifest.actor_id,
        project_id=manifest.project_id,
        selection_digest=manifest.selection_digest,
        sealed_digest="f" * 64,
    )
    with pytest.raises(ValueError):
        c.require_scope(request, page)
    crossed = page.model_dump()
    crossed["members"][0]["input"]["actor_id"] = uuid4()
    with pytest.raises(ValidationError):
        c.ManifestPage.model_validate(crossed)


def test_historical_ready_outcome_survives_later_input_expiry():
    manifest = header()
    value = member(manifest, 1, state="ready").model_dump()
    value["input"]["status"] = "expired"
    assert c.ManifestMember.model_validate(value).state == "ready"
    value["input"]["sha256"] = None
    value["input"]["byte_size"] = None
    with pytest.raises(ValidationError):
        c.ManifestMember.model_validate(value)


def test_native_observation_cannot_mix_identity_and_failure():
    request = c.PrepareManifestMemberRequest(
        **scope(), selection_digest="a" * 64, ordinal=1, task_id=uuid4()
    )
    base = {
        **request.model_dump(),
        "source_snapshot": "b" * 64,
        "object_identity": None,
        "terminal_error": None,
    }
    with pytest.raises(ValidationError):
        c.NativePrepareManifestMemberRequest.model_validate(base)
    valid = {**base, "object_identity": {"etag": "opaque", "byte_size": 200}}
    assert (
        c.NativePrepareManifestMemberRequest.model_validate(valid).object_identity.etag
        == "opaque"
    )
    with pytest.raises(ValidationError):
        c.NativePrepareManifestMemberRequest.model_validate(
            {**valid, "terminal_error": "source_changed"}
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"aap-assets-input-manifest/v1","ok":true,"data":NaN}',
        b"\xff",
        b" " * (c.REPLY_BYTES + 1),
    ],
)
def test_decoder_rejects_nonfinite_invalid_and_oversized_json(raw):
    with pytest.raises(c.ManifestFailure) as failure:
        c.decode_reply(raw, c.ManifestHeader)
    assert failure.value.code == "invalid_response"


def test_schema_exports_five_function_only_bindings_and_frozen_source_values():
    document = c.contract_document()
    assert json.loads(json.dumps(document))["$id"] == c.MANIFEST_SCHEMA
    assert len(document["x-native"]) == 5
    assert [op.role for op in c.OPERATIONS.values()].count("workflow") == 1
    assert [op.role for op in c.OPERATIONS.values()].count("control") == 4
    assert "ImageSource" in document["$defs"] and "InputRecord" in document["$defs"]
