"""SFTP references require deployment qualification and retain immutable bytes."""

from contextlib import contextmanager
import io
import os
import uuid

import paramiko
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.config import settings, SftpReferenceRoot
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.storage_connection import StorageConnection
from app.schemas.dataset_source import DatasetSourceCreate
from app.services import (
    dataset_scans as scans,
    dataset_content as content,
    storage_location as locations,
    connector_guard,
)
from app.services.content_snapshot import ContentError
from app.services.dataset_references import register_entry, reference_snapshot
from app.services.filesystem_storage import FilesystemStorageError
from app.services.source_readers import sftp as backend


def wire_attributes(info, filename=None):
    result = paramiko.SFTPAttributes.from_stat(info, filename=filename)
    result.st_mtime = int(result.st_mtime)
    return result


class MemoryFile(io.BytesIO):
    def __init__(self, path):
        super().__init__(path.read_bytes())
        self.path = path

    def stat(self):
        return wire_attributes(self.path.stat())

    def settimeout(self, value):
        pass


class MemorySftp:
    def __init__(self, root):
        self.root = root

    def path(self, path):
        return self.root / path.lstrip("/")

    def lstat(self, path):
        return wire_attributes(self.path(path).lstat())

    def listdir_iter(self, path, read_aheads):
        for item in self.path(path).iterdir():
            yield wire_attributes(item.lstat(), filename=item.name)

    def open(self, path, mode, bufsize):
        assert mode == "rb"
        return MemoryFile(self.path(path))


@pytest.fixture
async def sftp_world(db_session, super_admin, monkeypatch, tmp_path):
    root = tmp_path / "jail"
    (root / "collection" / "子目录").mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (48, 32), "navy").save(image, format="PNG")
    (root / "collection" / " 图片 +%2F.png").write_bytes(image.getvalue())
    (root / "collection" / "子目录" / "第二.png").write_bytes(image.getvalue())
    key = paramiko.RSAKey.generate(1024)
    profile = SftpReferenceRoot(
        host="10.1.1.1",
        username="reader",
        host_key=f"{key.get_name()} {key.get_base64()}",
    )
    monkeypatch.setattr(settings, "storage_sftp_roots", {"test": profile})
    memory = MemorySftp(root)

    @contextmanager
    def client(self):
        yield memory

    async def allowed(*args):
        pass

    monkeypatch.setattr(backend.SftpAccess, "client", client)
    monkeypatch.setattr(connector_guard, "assert_connection_target_allowed", allowed)
    actor = super_admin[0]
    connection = StorageConnection(
        name=f"sftp-{uuid.uuid4()}",
        kind="sftp",
        config={"host": profile.host, "username": profile.username, "base_path": "/"},
        secret_enc=b"sealed",
        scope="owner",
        created_by=actor.id,
    )
    dataset = Dataset(
        display_id=f"sftp-{uuid.uuid4().hex[:10]}",
        name="SFTP reference",
        created_by=actor.id,
    )
    db_session.add_all([connection, dataset])
    await db_session.flush()
    location = await locations.register_connection(db_session, connection)
    for node in ["worker:content@test", locations.api_node_id()]:
        result = await locations.perform_probe(
            db_session, location, node, "collection/ 图片 +%2F.png"
        )
        assert result["ok"], result
        from datetime import datetime, timezone

        await locations.store_probe(
            db_session, location, node, result, datetime.now(timezone.utc)
        )
    source = await scans.create_source(
        db_session,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="SFTP",
            relative_path="collection",
            node_id="content@test",
            cache_policy="none",
        ),
        actor,
    )
    return root, memory, actor, dataset, location, source


async def scan(db, world):
    run, _ = await scans.start_scan(db, world[-1].id, world[2])
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    for _ in range(30):
        work = await scans.next_directory(db, run.id, job.celery_task_id, fence)
        await db.commit()
        if work is None:
            return run
        if work.get("transition"):
            continue
        manifest = work["config"].scan_directory(
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.apply_directory(
            db, run.id, job.celery_task_id, fence, work, manifest
        )
        await db.commit()
    raise AssertionError("SFTP scan did not complete")


async def first_entry(db, run):
    return await db.scalar(
        select(DatasetScanEntry)
        .where(DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file")
        .order_by(DatasetScanEntry.path_digest)
    )


async def test_register_qualified_jail_without_copies_and_reject_links(
    db_session, sftp_world
):
    root, _, actor, dataset, location, _ = sftp_world
    (root / "collection" / "escape.png").symlink_to("/etc/passwd")
    run = await scan(db_session, sftp_world)
    assert run.complete and run.counts["file_count"] == 2
    assert run.counts["skipped_count"] == 1
    entries = list(
        await db_session.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
            )
        )
    )
    for entry in entries:
        result = await register_entry(
            db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
        )
        snapshot = await reference_snapshot(db_session, result["item_id"], actor)
        assert (
            snapshot.file.read()
            == (root / "collection" / entry.relative_path).read_bytes()
        )
        snapshot.close()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        == 2
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetAssetLocation)
            .where(
                DatasetAssetLocation.asset_id.in_(
                    select(DatasetAsset.id)
                    .join(DatasetItem, DatasetItem.id == DatasetAsset.dataset_item_id)
                    .where(DatasetItem.dataset_id == dataset.id)
                )
            )
        )
        == 0
    )
    assert (await locations.to_out(db_session, location, actor))[
        "declared_capabilities"
    ]["reference"]


async def test_same_stat_changed_bytes_cannot_replace_reference(db_session, sftp_world):
    root, _, actor, *_ = sftp_world
    run = await scan(db_session, sftp_world)
    entry = await first_entry(db_session, run)
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    path = root / "collection" / entry.relative_path
    info = path.stat()
    path.write_bytes(path.read_bytes().replace(b"IDAT", b"JDAT", 1))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ContentError, match="content_mismatch"):
        await reference_snapshot(db_session, result["item_id"], actor)


async def test_removed_qualification_blocks_new_sources_and_receipt_publication(
    db_session, sftp_world, monkeypatch
):
    _, _, actor, dataset, location, _ = sftp_world
    run = await scan(db_session, sftp_world)
    entry = await first_entry(db_session, run)
    original = content.snapshots.capture

    async def revoked(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        monkeypatch.setattr(settings, "storage_sftp_roots", {})
        return snapshot

    monkeypatch.setattr(content.snapshots, "capture", revoked)
    entry_id = (run.id, entry.path_digest)
    with pytest.raises(FilesystemStorageError, match="sftp_root_not_qualified"):
        await content.scan_snapshot(
            db_session,
            run.id,
            entry.path_digest,
            actor,
            verify=True,
            node_id="worker:content@test",
        )
    await db_session.rollback()
    current = await db_session.get(DatasetScanEntry, entry_id, populate_existing=True)
    assert current.verified_content is None


async def test_qualification_requires_exact_account_and_jail_root(sftp_world):
    _, _, _, _, _, _ = sftp_world
    config = {"host": "10.1.1.1", "username": "reader", "base_path": "/"}
    for override in [
        {"host": "10.1.1.2"},
        {"username": "other"},
        {"base_path": "/data"},
        {"port": 2222},
    ]:
        with pytest.raises(FilesystemStorageError, match="sftp_root_not_qualified"):
            backend.qualified_root({**config, **override})


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../secret", "a/../b", "a//b", "a\\b", "a\x00b"]
)
async def test_unsafe_relative_paths_rejected(sftp_world, path):
    root, memory, *_ = sftp_world
    access = backend.SftpAccess({}, b"sealed", (), "test", {})
    with pytest.raises(FilesystemStorageError):
        with access.content_file("collection", path):
            pass


async def test_single_directory_budget_and_symlink_open_fail_closed(
    db_session, sftp_world, monkeypatch
):
    from app.services.reference_storage import configuration

    root, _, _, _, location, _ = sftp_world
    access = await configuration(db_session, location)
    (root / "collection" / "link.png").symlink_to("/etc/passwd")
    with pytest.raises(FilesystemStorageError, match="symlink_rejected"):
        with access.content_file("collection", "link.png"):
            pass
    monkeypatch.setattr(backend, "MAX_ENTRIES", 1)
    with pytest.raises(FilesystemStorageError, match="directory_too_large"):
        access.scan_directory("collection", expected_anchor=access.anchor)


async def test_cancel_resume_revalidates_directories_and_rejects_old_lease(
    db_session, sftp_world
):
    actor, source = sftp_world[2], sftp_world[-1]
    run, _ = await scans.start_scan(db_session, source.id, actor)
    await db_session.commit()
    job = await db_session.get(AsyncJob, run.async_job_id)
    task_id = job.celery_task_id
    fence = await scans.claim(db_session, run.id, task_id, "content@test")
    work = await scans.next_directory(db_session, run.id, task_id, fence)
    while work.get("transition"):
        work = await scans.next_directory(db_session, run.id, task_id, fence)
    manifest = work["config"].scan_directory(
        work["path"], expected_anchor=work["anchor"]
    )
    await scans.apply_directory(db_session, run.id, task_id, fence, work, manifest)
    await db_session.commit()
    run_id = run.id
    await scans.cancel_job(db_session, job.id, actor)
    await db_session.commit()
    await scans.resume_scan(db_session, run.id, actor)
    await db_session.commit()
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(db_session, run_id, task_id, fence, work, manifest)
    await db_session.rollback()
    from app.db.models.dataset_source import DatasetScanRun

    run = await db_session.get(DatasetScanRun, run_id)
    job = await db_session.get(AsyncJob, run.async_job_id)
    task_id = job.celery_task_id
    fence = await scans.claim(db_session, run.id, task_id, "content@test")
    for _ in range(30):
        work = await scans.next_directory(db_session, run.id, task_id, fence)
        await db_session.commit()
        if work is None:
            break
        if work.get("transition"):
            continue
        manifest = work["config"].scan_directory(
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.apply_directory(db_session, run.id, task_id, fence, work, manifest)
        await db_session.commit()
    assert run.complete and run.counts["file_count"] == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetScanEntry)
            .where(DatasetScanEntry.run_id == run_id, DatasetScanEntry.kind == "file")
        )
        == 2
    )


async def test_directory_mutation_before_final_validation_is_incomplete(
    db_session, sftp_world
):
    root, _, actor, _, _, source = sftp_world
    run, _ = await scans.start_scan(db_session, source.id, actor)
    await db_session.commit()
    job = await db_session.get(AsyncJob, run.async_job_id)
    task_id = job.celery_task_id
    fence = await scans.claim(db_session, run.id, task_id, "content@test")
    work = await scans.next_directory(db_session, run.id, task_id, fence)
    while work.get("transition"):
        work = await scans.next_directory(db_session, run.id, task_id, fence)
    manifest = work["config"].scan_directory(
        work["path"], expected_anchor=work["anchor"]
    )
    await scans.apply_directory(db_session, run.id, task_id, fence, work, manifest)
    await db_session.commit()
    (root / "collection" / "late.png").write_bytes(b"new file")
    with pytest.raises(FilesystemStorageError, match="directory_changed"):
        for _ in range(30):
            work = await scans.next_directory(db_session, run.id, task_id, fence)
            if work is None:
                raise AssertionError("Changed listing was marked complete")
            if work.get("transition"):
                continue
            manifest = work["config"].scan_directory(
                work["path"],
                expected_anchor=work["anchor"],
                expected_identity=work["identity"],
            )
            await scans.apply_directory(
                db_session, run.id, task_id, fence, work, manifest
            )
            await db_session.commit()
    assert not run.complete


async def test_new_api_node_cannot_rebind_old_reference_to_new_qualification(
    db_session, sftp_world, monkeypatch
):
    from datetime import datetime, timezone

    root, memory, actor, _, location, _ = sftp_world
    run = await scan(db_session, sftp_world)
    entry = await first_entry(db_session, run)
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    key = paramiko.RSAKey.generate(1024)
    profile = settings.storage_sftp_roots["test"].model_copy(
        update={"host_key": f"{key.get_name()} {key.get_base64()}"}
    )
    monkeypatch.setattr(settings, "storage_sftp_roots", {"test": profile})
    monkeypatch.setattr(locations, "api_node_id", lambda: "api:new-node")
    proof = await locations.perform_probe(
        db_session, location, "api:new-node", "collection/ 图片 +%2F.png"
    )
    assert proof["ok"]
    await locations.store_probe(
        db_session, location, "api:new-node", proof, datetime.now(timezone.utc)
    )
    await db_session.commit()
    with pytest.raises(ContentError, match="content_binding_changed"):
        await reference_snapshot(db_session, result["item_id"], actor)


def test_ssh_uses_validated_socket_and_independent_pinned_host_keys(monkeypatch):
    from types import SimpleNamespace

    calls = []
    key = paramiko.RSAKey.generate(1024)
    root = {
        "host": "qualified.example",
        "port": 22,
        "username": "reader",
        "host_key": f"{key.get_name()} {key.get_base64()}",
    }

    class Socket:
        def settimeout(self, value):
            pass

        def connect(self, address):
            calls.append(("socket", address))

        def close(self):
            calls.append(("socket_closed",))

    sock = Socket()

    class Client:
        def get_host_keys(self):
            return paramiko.HostKeys()

        def set_missing_host_key_policy(self, policy):
            assert isinstance(policy, paramiko.RejectPolicy)

        def connect(self, **kwargs):
            assert kwargs["sock"] is sock and kwargs["hostname"] == root["host"]
            assert not kwargs["allow_agent"] and not kwargs["look_for_keys"]
            calls.append(("ssh",))

        def open_sftp(self):
            return SimpleNamespace(
                get_channel=lambda: SimpleNamespace(settimeout=lambda v: None),
                close=lambda: None,
            )

        def close(self):
            calls.append(("ssh_closed",))

    monkeypatch.setattr(backend.paramiko, "SSHClient", Client)
    monkeypatch.setattr(backend.socket, "socket", lambda *args: sock)
    monkeypatch.setattr(
        connector_guard, "resolve_allowed_ips", lambda *args: ["10.2.3.4"]
    )
    monkeypatch.setattr(backend, "decrypt_secret", lambda value: {"password": "test"})
    with backend.SftpAccess({}, b"sealed", (), "test", root).client():
        pass
    assert calls == [
        ("socket", ("10.2.3.4", 22)),
        ("ssh",),
        ("ssh_closed",),
        ("socket_closed",),
    ]


@pytest.mark.parametrize("encrypted", [False, True])
def test_bad_private_key_is_a_connection_failure(monkeypatch, encrypted):
    key = paramiko.RSAKey.generate(1024)
    secret = {"private_key": "invalid private key"}
    if encrypted:
        pem = io.StringIO()
        key.write_private_key(pem, password="correct-test-passphrase")
        secret = {"private_key": pem.getvalue(), "passphrase": "wrong-test-passphrase"}
    root = {
        "host": "qualified.example",
        "port": 22,
        "username": "reader",
        "host_key": f"{key.get_name()} {key.get_base64()}",
    }
    monkeypatch.setattr(
        connector_guard, "resolve_allowed_ips", lambda *args: ["10.2.3.4"]
    )
    monkeypatch.setattr(backend, "decrypt_secret", lambda value: secret)
    result = backend.SftpAccess({}, b"sealed", (), "test", root).probe()
    assert not result["ok"]
    assert result["code"] == "connection_unavailable"
