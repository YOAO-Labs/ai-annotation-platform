"""Creation's saved-source and first-deadline contract without database I/O."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sys
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app import services
from app.services import managed_original_creation as creation
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_managed_original_creation import Client


class ImportError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code


@pytest.fixture
def adapter(monkeypatch):
    fake = SimpleNamespace(ImportStorageError=ImportError)
    monkeypatch.setattr(services, "import_storage", fake, raising=False)
    monkeypatch.setitem(sys.modules, "app.services.import_storage", fake)
    return fake


@pytest.fixture
def allocated():
    identifier = uuid.uuid4()
    saved = {
        "endpoint": "http://saved-storage.example:9000",
        "buckets": {"import": "original-stage", "datasets": "final"},
        "bindings": [{"purpose": "import", "generation": 1}],
    }
    return {
        "id": identifier,
        "origin": "direct",
        "configuration": saved,
        "input_fingerprint": creation._digest(saved),
        "staging_ref": None,
        "staging_bucket": "original-stage",
        "staging_key": f"__aap_upload_staging__/{identifier}/payload",
        "content_type": "image/png",
        "allocated_at": datetime(2026, 10, 25, tzinfo=timezone.utc),
    }


def saved_owner(allocated):
    key = allocated["staging_key"]
    return SimpleNamespace(
        bucket=allocated["staging_bucket"],
        key=lambda _: key,
    )


def test_null_ref_uses_saved_endpoint_bucket_and_exact_creation_prefix(
    adapter, allocated
):
    reference, owner = object(), saved_owner(allocated)
    adapter.legacy_ref = Mock(return_value=reference)
    adapter.resolve_ref = Mock(return_value=owner)
    assert creation.staging_owner(allocated) is owner
    adapter.legacy_ref.assert_called_once_with(
        f"__aap_upload_staging__/{allocated['id']}/",
        endpoint_url="http://saved-storage.example:9000",
        bucket="original-stage",
    )
    adapter.resolve_ref.assert_called_once_with(
        reference, expected_prefix=f"__aap_upload_staging__/{allocated['id']}/"
    )


@pytest.mark.parametrize("change", ["digest", "saved_bucket", "missing_bucket"])
def test_null_ref_cannot_invent_missing_or_tampered_legacy_facts(
    adapter, allocated, change
):
    adapter.legacy_ref = Mock()
    if change == "digest":
        allocated["configuration"]["unrelated"] = "tampered"
    else:
        allocated["configuration"]["buckets"].pop("import")
        if change == "saved_bucket":
            allocated["configuration"]["buckets"]["import"] = "other-stage"
        allocated["input_fingerprint"] = creation._digest(allocated["configuration"])
    with pytest.raises(ContentError, match="managed_original_configuration_changed"):
        creation.staging_owner(allocated)
    adapter.legacy_ref.assert_not_called()


@pytest.mark.parametrize("field", ["staging_bucket", "staging_key"])
def test_saved_ref_must_still_match_redundant_bucket_and_key(adapter, allocated, field):
    allocated["staging_ref"] = {"saved": "reference"}
    adapter.resolve_ref = Mock(return_value=saved_owner(allocated))
    creation.staging_owner(allocated)
    allocated[field] = "forged-current-stage"
    with pytest.raises(ContentError, match="managed_original_creation_input_changed"):
        creation.staging_owner(allocated)


def test_comparison_excludes_import_but_digest_remains_complete(allocated):
    saved = allocated["configuration"]
    current = deepcopy(saved)
    current["buckets"]["import"] = "new-stage"
    current["bindings"][0]["generation"] = 2
    assert creation._original_configuration(saved) == creation._original_configuration(
        current
    )
    assert creation._digest(saved) != creation._digest(current)
    current["buckets"]["datasets"] = "other-final"
    assert creation._original_configuration(saved) != creation._original_configuration(
        current
    )


async def test_validation_rejects_tampered_unconsumed_bucket_from_saved_digest(
    allocated,
):
    row = allocated | {
        "origin": "zip",
        "location_id": uuid.uuid4(),
        "maintenance_revision": 1,
    }
    # The stored snapshot digest covers the complete saved configuration,
    # including purposes this writer no longer resolves as dependencies.
    row["configuration"]["buckets"]["import"] = "forged-old-bucket"
    with pytest.raises(ContentError, match="managed_original_configuration_changed"):
        await creation._validate(None, row)


async def test_retry_upload_url_keeps_first_creation_deadline(
    adapter, allocated, monkeypatch
):
    owner = saved_owner(allocated)
    monkeypatch.setattr(creation, "staging_owner", lambda _: owner)
    monkeypatch.setattr(
        creation, "now", lambda: allocated["allocated_at"] + timedelta(seconds=300)
    )
    adapter.prepare_upload_url = AsyncMock(return_value="signed")
    assert await creation.prepare_upload_url(None, allocated) == "signed"
    adapter.prepare_upload_url.assert_awaited_once_with(
        None, owner, "payload", content_type="image/png", expires_in=600
    )


async def test_expired_creation_never_issues_new_url(adapter, allocated, monkeypatch):
    monkeypatch.setattr(creation, "staging_owner", lambda _: saved_owner(allocated))
    monkeypatch.setattr(
        creation, "now", lambda: allocated["allocated_at"] + timedelta(seconds=900)
    )
    adapter.prepare_upload_url = AsyncMock()
    with pytest.raises(ContentError, match="managed_original_staging_expired"):
        await creation.prepare_upload_url(None, allocated)
    adapter.prepare_upload_url.assert_not_awaited()


async def test_final_url_validation_is_bound_to_source_key_and_type(
    adapter, allocated, monkeypatch
):
    owner = saved_owner(allocated)
    monkeypatch.setattr(creation, "staging_owner", lambda _: owner)
    adapter.validate_upload_url = AsyncMock(return_value=1)
    assert await creation.validate_upload_url(None, allocated, "signed") == 1
    adapter.validate_upload_url.assert_awaited_once_with(
        None,
        "signed",
        owner=owner,
        relative="payload",
        content_type="image/png",
        lock=True,
    )


async def test_provider_failure_retains_unsettled_status_and_safe_code(
    adapter, allocated, monkeypatch
):
    monkeypatch.setattr(creation, "staging_owner", lambda _: saved_owner(allocated))
    monkeypatch.setattr(creation, "now", lambda: allocated["allocated_at"])
    error = creation.storage_access.mark_unsettled(
        ImportError("import_storage_unavailable", 503)
    )
    adapter.prepare_upload_url = AsyncMock(side_effect=error)
    with pytest.raises(ContentError) as caught:
        await creation.prepare_upload_url(None, allocated)
    assert caught.value.code == "import_storage_unavailable"
    assert caught.value.status == 503
    assert caught.value.storage_operation_unsettled


@pytest.mark.parametrize("source_close_failure", [False, True])
async def test_source_client_close_never_skips_destination_close(
    allocated, monkeypatch, source_close_failure
):
    reader, destination = Client(), Client()
    reader.close_failure = source_close_failure
    reader.store(allocated["staging_bucket"], allocated["staging_key"], b"original")
    row = allocated | {
        "bucket": "final",
        "object_key": "original",
        "location_id": LEGACY_LOCATION_ID,
        "max_bytes": 8,
        "timeout_seconds": 60,
        "owner_kind": "dataset",
    }

    @contextmanager
    def client():
        try:
            yield reader
        finally:
            reader.close()

    async def change(_db, _identifier, _expected, *, record_mutation=False, **values):
        result = row | values
        return (result, object()) if record_mutation else result

    publication = AsyncMock()
    monkeypatch.setattr(creation, "create_client", lambda **_: destination)
    monkeypatch.setattr(creation, "_change", change)
    monkeypatch.setattr(creation, "_validate", AsyncMock())
    monkeypatch.setattr(creation.storage_access, "engine", lambda _: object())
    monkeypatch.setattr(creation, "async_sessionmaker", lambda *_, **__: object())
    monkeypatch.setattr(creation.accounting, "publish", publication)
    if source_close_failure:
        with pytest.raises(ContentError) as caught:
            await creation._physical(
                None, row, io.BytesIO(), source_target=SimpleNamespace(client=client)
            )
        assert caught.value.code == "managed_original_close_unknown"
        assert caught.value.storage_operation_unsettled
    else:
        assert (
            await creation._physical(
                None, row, io.BytesIO(), source_target=SimpleNamespace(client=client)
            )
            == b"original"
        )
    assert reader.closed == destination.closed == 1
    assert destination.objects["final", "original"]["data"] == b"original"
    assert publication.await_args.kwargs["settled"] is not source_close_failure
