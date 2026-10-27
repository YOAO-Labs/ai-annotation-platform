"""Frozen source reads expose no mutation or signing capability."""

import asyncio
import io
from types import SimpleNamespace
import uuid

import pytest
from botocore.hooks import HierarchicalEmitter

from app.services import storage_migration_source_read as read
from app.services import storage_access

SOURCE = {
    "endpoint": "http://source.example:80/",
    "purpose_buckets": {"datasets": "frozen"},
}


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(
            endpoint_url="http://source.example", events=HierarchicalEmitter()
        )
        self.calls = []
        self.body = io.BytesIO(b"source")

    def __getattr__(self, name):
        def call(**params):
            self.calls.append((name, params))
            return {"Body": self.body}

        return call


@pytest.mark.parametrize("method", sorted(read.INVENTORY_METHODS))
def test_inventory_allows_exact_seven_methods_without_owning_response(method):
    client = Client()
    response = getattr(read.SourceRead(client, SOURCE, owner_kind="inventory"), method)(
        Bucket="frozen", Key="exact/%2F/中文"
    )
    assert response["Body"] is client.body and not client.body.closed
    assert client.calls == [(method, {"Bucket": "frozen", "Key": "exact/%2F/中文"})]


@pytest.mark.parametrize("kind", ["inventory", "copy"])
@pytest.mark.parametrize(
    "method",
    [
        "put_object",
        "delete_object",
        "delete_objects",
        "upload_file",
        "create_multipart_upload",
        "generate_presigned_url",
        "close",
    ],
)
def test_read_capability_rejects_all_write_sign_and_owner_methods(kind, method):
    client = Client()
    with pytest.raises(read.SourceReadError):
        getattr(read.SourceRead(client, SOURCE, owner_kind=kind), method)(
            Bucket="frozen"
        )
    assert client.calls == []


@pytest.mark.parametrize("method", sorted(read.INVENTORY_METHODS - read.COPY_METHODS))
def test_copy_exposes_only_head_and_get(method):
    client = Client()
    with pytest.raises(read.SourceReadError):
        getattr(read.SourceRead(client, SOURCE, owner_kind="copy"), method)(
            Bucket="frozen"
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "params",
    [{}, {"Bucket": "frozen-neighbor"}, {"Bucket": None}, {"Bucket": ["frozen"]}],
)
def test_exact_bucket_is_required_before_any_provider_call(params):
    client = Client()
    source = read.SourceRead(client, SOURCE, owner_kind="copy")
    with pytest.raises(read.SourceReadError):
        source.get_object(**params)
    assert client.calls == []


def test_positional_bucket_and_endpoint_drift_cannot_escape_frozen_scope():
    client = Client()
    source = read.SourceRead(client, SOURCE, owner_kind="copy")
    with pytest.raises(read.SourceReadError):
        source.head_object("frozen")
    client.meta.endpoint_url = "https://source.example"
    with pytest.raises(read.SourceReadError):
        source.head_object(Bucket="frozen")
    assert client.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [None, "http://other.example", "http://u:p@source.example", "source.example"],
)
def test_wrong_or_invalid_endpoint_is_rejected_before_io(endpoint):
    client = Client()
    client.meta.endpoint_url = endpoint
    with pytest.raises(read.SourceReadError):
        read.SourceRead(client, SOURCE, owner_kind="copy")
    assert client.calls == []


def test_invalid_frozen_endpoint_is_not_equal_to_missing_client_endpoint():
    client = Client()
    client.meta.endpoint_url = None
    with pytest.raises(read.SourceReadError):
        read.SourceRead(client, {**SOURCE, "endpoint": None}, owner_kind="copy")


@pytest.mark.parametrize(
    "url",
    [
        "https://source.example/frozen/key",
        "http://other.example/frozen/key",
        "http://u:p@source.example/frozen/key",
    ],
)
def test_actual_transport_redirect_is_rejected_by_public_send_hook(url):
    client = Client()
    read.SourceRead(client, SOURCE, owner_kind="copy")
    with pytest.raises(read.SourceReadError):
        client.meta.events.emit(
            "before-send.s3.GetObject", request=SimpleNamespace(url=url)
        )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?versioning",
        "?versions&key-marker=a",
        "?list-type=2&continuation-token=x",
        "?versionId=immutable%2Fversion",
        "?uploads&key-marker=a",
    ],
)
def test_same_origin_hook_observes_without_modifying_request(query):
    client = Client()
    read.SourceRead(client, SOURCE, owner_kind="copy")
    original = "http://source.example/frozen/key" + query
    request = SimpleNamespace(url=original)
    for _, result in client.meta.events.emit(
        "before-send.s3.GetObject", request=request
    ):
        assert result is None
    assert request.url == original


@pytest.mark.parametrize("late_failure", [False, True])
async def test_new_cancellation_during_settlement_survives_existing_native_error(
    monkeypatch, late_failure
):
    from app.services import storage_migration_windows as windows

    permit = storage_access.Permit(None, uuid.uuid4(), uuid.uuid4(), 2)
    entered, release = asyncio.Event(), asyncio.Event()
    native_error = ValueError("native business failed")

    async def settle():
        entered.set()
        await release.wait()
        if late_failure:
            raise storage_access.mark_unsettled(OSError("settlement uncertain"))
        permit._settled = True

    async def acquire(*args, **kwargs):
        return permit

    def native():
        raise native_error

    permit._settle = settle
    monkeypatch.setattr(windows, "acquire_source_read", acquire)
    task = asyncio.create_task(
        read.run(None, uuid.uuid4(), "inventory", uuid.uuid4(), "owned-task", native)
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert (
        bool(getattr(caught.value, "storage_operation_unsettled", False))
        is late_failure
    )
    assert permit._settled is not late_failure
