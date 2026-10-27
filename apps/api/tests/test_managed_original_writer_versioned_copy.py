"""Versioned staging on services which omit the server-copy version echo."""

from types import SimpleNamespace

import pytest

from app.services import managed_original_writer as writer
from tests.test_managed_original_writer import Body, MemoryS3, check_error


@pytest.fixture
def copy_world(monkeypatch):
    client = MemoryS3()
    client.versioned = True
    data = b"original"
    client.store("stage", "source", data, content_type="image/png")
    source = writer.inspect_staging(client, "stage", "source", max_bytes=len(data))
    # Only staging is versioned; the target returns its genuine null receipt.
    client.versioned = False
    copy = client.upload_part_copy

    def no_echo(**params):
        result = copy(**params)
        result.pop("CopySourceVersionId")
        return result

    monkeypatch.setattr(client, "upload_part_copy", no_echo)
    client.calls.clear()

    def prepare(**kwargs):
        return writer.prepare_copy(
            client,
            "stage",
            "source",
            "owned",
            "final",
            source,
            content_type="image/png",
            **kwargs,
        )

    return SimpleNamespace(client=client, source=source, data=data, prepare=prepare)


def test_missing_echo_streams_exact_version_before_conditional_publication(copy_world):
    world = copy_world
    client = world.client
    prepared = world.prepare()
    assert prepared.sha256 == world.source.sha256
    assert prepared.md5 == world.source.md5 and prepared.head_bytes == world.data
    assert prepared.size_bytes == len(world.data)
    operations = [operation for operation, _ in client.calls]
    assert operations.index("abort") < operations.index("get")
    assert operations.count("abort") == 1 and operations.count("create") == 2
    assert set(client.uploads) == {prepared.upload_id}
    gets = [params for operation, params in client.calls if operation == "get"]
    assert gets and all(p["VersionId"] == world.source.version_id for p in gets)
    assert gets[-1]["IfMatch"] == world.source.etag
    receipt = writer.complete(
        client, "owned", "final", prepared, content_type="image/png"
    )
    assert receipt.version_id is None
    verified = writer.verify(
        client,
        "owned",
        "final",
        receipt,
        sha256=world.source.sha256,
        size_bytes=len(world.data),
        max_bytes=len(world.data),
    )
    assert verified.sha256 == world.source.sha256
    assert client.objects["owned", "final"]["data"] == world.data
    assert not client.uploads and all(body.close_count == 1 for body in client.bodies)


@pytest.mark.parametrize("echo", [None, "null", "", "different-version"])
def test_explicit_version_disagreement_never_falls_back(copy_world, monkeypatch, echo):
    client = copy_world.client
    copy = client.upload_part_copy

    def different(**params):
        result = copy(**params)
        result["CopySourceVersionId"] = echo
        return result

    monkeypatch.setattr(client, "upload_part_copy", different)
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare()
    check_error(caught)
    assert [op for op, _ in client.calls] == ["create", "copy", "abort"]
    assert not client.uploads


@pytest.mark.parametrize("raw", [b"modified", b"short", b"too-long-source"])
def test_fallback_requires_exact_frozen_bytes_and_aborts_prepared_parts(
    copy_world, monkeypatch, raw
):
    client = copy_world.client
    get = client.get_object

    def altered(**params):
        result = get(**params)
        result["Body"].close()
        result["Body"] = Body(raw)
        client.bodies.append(result["Body"])
        return result

    monkeypatch.setattr(client, "get_object", altered)
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare()
    check_error(caught)
    assert not client.uploads and ("owned", "final") not in client.objects
    assert all(body.close_count == 1 for body in client.bodies)
    assert not any(op == "complete" for op, _ in client.calls)


def test_fallback_rejects_same_content_new_current_version(copy_world):
    client = copy_world.client

    def changed(operation, params):
        if operation == "part":
            client.versioned = True
            client.store("stage", "source", copy_world.data, content_type="image/png")

    client.hook = changed
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare()
    check_error(caught, code="creation_source_changed")
    assert not client.uploads and ("owned", "final") not in client.objects
    assert [op for op, _ in client.calls].count("abort") == 2
    assert all(body.close_count == 1 for body in client.bodies)


def test_unknown_copied_mpu_abort_never_starts_fallback(copy_world):
    client = copy_world.client

    def abort_failure(operation, params):
        if operation == "abort":
            raise OSError("private abort detail")

    client.hook = abort_failure
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare()
    check_error(caught, "unknown", "creation_abort_unsettled")
    assert [op for op, _ in client.calls] == ["create", "copy", "abort"]
    assert len(client.uploads) == 1


def test_unknown_fallback_body_close_aborts_new_parts_and_retains_uncertainty(
    copy_world,
):
    client = copy_world.client

    def close_failure(body, chunk):
        body.close_failure = True

    client.body_hook = close_failure
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare()
    check_error(caught, "unknown", "creation_close_unsettled")
    assert not client.uploads and ("owned", "final") not in client.objects
    assert [op for op, _ in client.calls].count("abort") == 2
    assert all(body.close_count == 1 for body in client.bodies)


def test_copy_and_fallback_share_the_original_deadline(copy_world, monkeypatch):
    client = copy_world.client
    clock = [0]
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])

    def copy_took_time(operation, params):
        if operation == "copy":
            clock[0] = 6

    def read_exceeds_original_deadline(body, chunk):
        clock[0] = 11

    client.hook = copy_took_time
    client.body_hook = read_exceeds_original_deadline
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare(timeout_seconds=10)
    check_error(caught, code="creation_timeout")
    assert not client.uploads and ("owned", "final") not in client.objects
    assert all(body.close_count == 1 for body in client.bodies)


def test_late_fallback_mpu_creation_never_starts_a_part(copy_world, monkeypatch):
    client = copy_world.client
    clock, creates = [0], []
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])

    def elapsed(operation, params):
        if operation == "copy":
            clock[0] = 6
        if operation == "create":
            creates.append(params)
            if len(creates) == 2:
                clock[0] = 11

    client.hook = elapsed
    with pytest.raises(writer.WriterError) as caught:
        copy_world.prepare(timeout_seconds=10)
    check_error(caught, code="creation_timeout")
    assert not any(op == "part" for op, _ in client.calls)
    assert [op for op, _ in client.calls].count("abort") == 2
    assert not client.uploads and ("owned", "final") not in client.objects
    assert all(body.close_count == 1 for body in client.bodies)
