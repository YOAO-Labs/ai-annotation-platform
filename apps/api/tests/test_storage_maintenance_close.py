"""Provider cleanup proves closure; failed close preserves unresolved admission."""

from contextlib import contextmanager
import io
import threading
from types import SimpleNamespace

import paramiko
import pytest

from app.config import settings
from aap_assets_source.connectors import probe as probe_module
from app.services import filesystem_storage as fs, storage_access
from app.services.source_readers import s3 as s3_reference
from app.services.source_readers import sftp as sftp_reference
from aap_assets_source.connectors.s3 import S3CompatibleSource
from aap_assets_source.connectors.sftp import SftpSource
from tests.test_s3_references import MemoryS3
from tests.test_sftp_references import MemoryFile, MemorySftp


def unsettled(error):
    assert getattr(error.value, "storage_operation_unsettled", False)


def test_filesystem_closes_all_descriptors_after_a_close_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"owned": str(tmp_path)})
    original = fs.os.close
    closed = []

    def close(fd):
        original(fd)
        closed.append(fd)
        if len(closed) == 1:
            raise OSError("uncertain close")

    monkeypatch.setattr(fs.os, "close", close)
    with pytest.raises(OSError) as error:
        fs.probe_directory({"root_handle": "owned", "relative_path": ""})
    unsettled(error)
    assert len(closed) >= 4 and len(closed) == len(set(closed))


@pytest.mark.parametrize("failure", ["body", "client", "read"])
def test_s3_reference_propagates_only_close_uncertainty(monkeypatch, failure):
    memory = MemoryS3()
    closed = []

    class Client:
        meta = SimpleNamespace(events=SimpleNamespace(register=lambda *args: None))

        def list_objects_v2(self, **kwargs):
            if failure == "read":
                raise ValueError("ordinary read failure")
            return memory.list_objects_v2(**kwargs)

        def head_object(self, **kwargs):
            return memory.head_object(**kwargs)

        def get_object(self, **kwargs):
            response = memory.get_object(**kwargs)
            data = response["Body"].read()
            response["Body"].close()
            response["Body"] = Body(data)
            return response

        def close(self):
            closed.append("client")
            if failure == "client":
                raise fs.FilesystemStorageError("close_failed")

    class Body(io.BytesIO):
        def close(self):
            if self.closed:
                return
            super().close()
            closed.append("body")
            if failure == "body":
                raise fs.FilesystemStorageError("close_failed")

    monkeypatch.setattr(s3_reference, "allowed_endpoint", lambda *args: None)
    monkeypatch.setattr(
        s3_reference,
        "decrypt_secret",
        lambda value: {"access_key": "test", "secret_key": "test"},
    )
    monkeypatch.setattr(s3_reference.boto3, "client", lambda *args, **kwargs: Client())
    access = s3_reference.S3Access(
        {"endpoint": "http://example.test", "bucket": "data", "base_prefix": "root"},
        b"sealed",
        (),
    )
    with pytest.raises((ValueError, fs.FilesystemStorageError)) as error:
        access.probe("collection/a//图片 +%2F.png")
    assert bool(getattr(error.value, "storage_operation_unsettled", False)) == (
        failure != "read"
    )
    assert "client" in closed
    if failure == "body":
        assert "body" in closed


@pytest.mark.parametrize("failure", ["sftp", "ssh", "socket", "read"])
def test_sftp_reference_client_closes_other_handles_and_preserves_marker(
    monkeypatch, failure
):
    calls = []
    key = paramiko.RSAKey.generate(1024)
    root = {
        "host": "qualified.test",
        "port": 22,
        "username": "reader",
        "host_key": f"{key.get_name()} {key.get_base64()}",
    }

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(self.name)
            if failure == self.name:
                raise OSError("uncertain close")

        def settimeout(self, value):
            pass

    class Socket(Resource):
        def connect(self, address):
            pass

    class Sftp(Resource):
        def get_channel(self):
            return self

    class SSH(Resource):
        def get_host_keys(self):
            return paramiko.HostKeys()

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def open_sftp(self):
            return Sftp("sftp")

    monkeypatch.setattr(sftp_reference.paramiko, "SSHClient", lambda: SSH("ssh"))
    monkeypatch.setattr(sftp_reference.socket, "socket", lambda *args: Socket("socket"))
    monkeypatch.setattr(
        sftp_reference.connector_guard,
        "resolve_allowed_ips",
        lambda *args: ["10.1.1.1"],
    )
    monkeypatch.setattr(
        sftp_reference, "decrypt_secret", lambda value: {"password": "test"}
    )
    with pytest.raises((OSError, fs.FilesystemStorageError)) as error:
        with sftp_reference.SftpAccess({}, b"sealed", (), "test", root).client():
            if failure == "read":
                raise OSError("ordinary read failure")
    assert bool(getattr(error.value, "storage_operation_unsettled", False)) == (
        failure != "read"
    )
    assert calls == ["sftp", "ssh", "socket"]


def test_sftp_probe_does_not_swallow_failed_body_close(tmp_path, monkeypatch):
    (tmp_path / "sample.bin").write_bytes(b"test")

    class Body(MemoryFile):
        def close(self):
            if self.closed:
                return
            super().close()
            raise fs.FilesystemStorageError("body_close_failed")

    class Memory(MemorySftp):
        def open(self, path, mode, bufsize):
            return Body(self.path(path))

    @contextmanager
    def client(self):
        yield Memory(tmp_path)

    monkeypatch.setattr(sftp_reference.SftpAccess, "client", client)
    access = sftp_reference.SftpAccess({}, b"sealed", (), "test", {})
    with pytest.raises(fs.FilesystemStorageError) as error:
        access.probe("sample.bin")
    unsettled(error)


def test_copy_adapter_closes_real_s3_client_and_failed_sftp_constructor(monkeypatch):
    closed = []
    monkeypatch.setattr(
        "aap_assets_source.connectors.s3.boto3.client",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: closed.append("s3")),
    )
    source = S3CompatibleSource({"endpoint": "example.test", "bucket": "data"}, {})
    source.close()

    class SSH:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            raise ValueError("ordinary authentication failure")

        def close(self):
            closed.append("ssh")

    monkeypatch.setattr(paramiko, "SSHClient", SSH)
    with pytest.raises(ValueError) as error:
        SftpSource({"host": "example.test", "username": "reader"}, {})
    assert not getattr(error.value, "storage_operation_unsettled", False)
    assert closed == ["s3", "ssh"]


@pytest.mark.parametrize("failure", [False, True])
def test_persisted_s3_probe_marks_close_failure_only(monkeypatch, failure):
    class Client:
        def list_objects_v2(self, **kwargs):
            if not failure:
                raise OSError("ordinary read failure")
            return {"KeyCount": 0}

        def close(self):
            if failure:
                raise OSError("close failed")

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: Client())
    with pytest.raises(OSError) as error:
        probe_module._probe_s3({"endpoint": "example.test", "bucket": "data"}, {})
    assert bool(getattr(error.value, "storage_operation_unsettled", False)) == failure


@pytest.mark.parametrize("close_fails", [False, True])
def test_deadline_waits_for_running_close_and_propagates_failure(
    monkeypatch, close_fails
):
    monkeypatch.setattr(probe_module, "PROBE_TIMEOUT_SECONDS", 0.01)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def close():
        started.set()
        assert release.wait(2)
        if close_fails:
            raise OSError("close failed")

    def execute():
        try:
            with probe_module._close_on_deadline(close):
                assert started.wait(1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert started.wait(1)
        assert not finished.wait(0.02)
    finally:
        release.set()
        thread.join(2)
    assert finished.is_set()
    assert bool(errors) == close_fails
    if close_fails:
        assert getattr(errors[0], "storage_operation_unsettled", False)


async def test_location_close_failure_is_safe_explicit_error(monkeypatch):
    from app.services import storage_location
    from app.services.content_snapshot import ContentError

    async def failed(*args, **kwargs):
        raise storage_access.mark_unsettled(OSError("private path and credentials"))

    monkeypatch.setattr(storage_access, "run", failed)
    with pytest.raises(ContentError) as error:
        await storage_location._registered_io()
    assert (
        error.value.code == "storage_operation_unsettled" and error.value.status == 503
    )
    unsettled(error)
