"""Registered references over real Source HTTP, PostgreSQL, S3 and SSH."""

import asyncio
from contextlib import closing, contextmanager
import errno
import hashlib
from io import BytesIO
import ipaddress
import os
import secrets
import socket
from threading import Event, Thread
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from cryptography.fernet import Fernet
import httpx
import paramiko
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset import Dataset
from app.db.models.storage_connection import StorageConnection
from app.deps import get_db
from app.schemas.dataset_source import DatasetSourceCreate
from app.services import dataset_scans as scans, reference_storage, storage_access
from app.services import storage_location as locations
from app.services.dataset_references import reference_source_context
from app.services.dataset_scan_data import read_scan
from app.services.storage_registry_data import get_location
from app.workers.dataset_scan import execute_scan
from tests.conftest import _create_user
from tests.saved_io_live_fixture import real_saved_io_source, required as _required
from yoao_assets_source.connectors.crypto import encrypt_secret
from yoao_assets_source.reference_protocol import SftpReferenceRoot
from yoao_assets_source.saved_io import SavedIOClient, SavedIOError

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_REFERENCE_IO_LIVE") != "1",
    reason="requires disposable restricted PostgreSQL roles, a private real S3 origin and SSH bind address",
)
_NODE = "reference-io@integration"


def _png(color):
    buffer = BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _Auth(paramiko.ServerInterface):
    def __init__(self, password):
        self.password = password

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        if username == "reader" and secrets.compare_digest(password, self.password):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Jail(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        self.root = root.resolve()

    def _path(self, path, *, follow):
        value = self.root.joinpath(path.lstrip("/"))
        if follow or value == self.root:
            value = value.resolve()
        else:
            value = value.parent.resolve() / value.name
        if not value.is_relative_to(self.root):
            raise PermissionError(errno.EACCES, "outside fixture jail")
        return value

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(
                self._path(path, follow=True).stat()
            )
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(
                self._path(path, follow=False).lstat()
            )
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def list_folder(self, path):
        try:
            return [
                paramiko.SFTPAttributes.from_stat(child.lstat(), filename=child.name)
                for child in self._path(path, follow=True).iterdir()
            ]
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            stream = self._path(path, follow=True).open("rb")
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

        class File(paramiko.SFTPHandle):
            def stat(self):
                return paramiko.SFTPAttributes.from_stat(
                    os.fstat(self.readfile.fileno())
                )

        handle = File(flags)
        handle.readfile = stream
        return handle


def _ssh_host_key(key):
    return f"{key.get_name()} {key.get_base64()}"


@contextmanager
def _sftp_world(path):
    host = _required("AAP_REFERENCE_IO_SFTP_HOST")
    ip = ipaddress.IPv4Address(host)
    if ip.is_loopback or not ip.is_private:
        pytest.fail("SFTP acceptance must bind a private, non-loopback fixture address")
    password = secrets.token_urlsafe(32)
    key = paramiko.RSAKey.generate(2048)
    root = path / "jail"
    collection = root / "collection"
    (collection / "branch").mkdir(parents=True)
    (collection / "other").mkdir()
    image = collection / "edge +%2F.png"
    image.write_bytes(_png("navy"))
    (collection / "skip.txt").write_text("not an image", encoding="utf-8")
    outside = path / "outside.png"
    outside.write_bytes(_png("white"))
    (collection / "outside.png").symlink_to(outside)
    stop = Event()
    sessions = []
    with closing(socket.socket()) as listener:
        listener.bind((host, 0))
        listener.listen()
        listener.settimeout(0.2)
        port = listener.getsockname()[1]

        def serve():
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                transport = paramiko.Transport(peer)
                sessions.append(transport)
                transport.add_server_key(key)
                transport.set_subsystem_handler(
                    "sftp", paramiko.SFTPServer, _Jail, root
                )
                try:
                    transport.start_server(server=_Auth(password))
                except (OSError, paramiko.SSHException, EOFError):
                    transport.close()

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            config = {
                "host": host,
                "port": port,
                "username": "reader",
                "base_path": "/",
                "auth_type": "password",
            }
            qualification = {
                field: config[field] for field in ("host", "port", "username")
            }
            yield (
                config,
                {"password": password},
                {**qualification, "host_key": _ssh_host_key(key)},
                image,
            )
        finally:
            stop.set()
            thread.join(timeout=2)
            for session in sessions:
                session.close()
            thread.join(timeout=2)
            assert not thread.is_alive()


@contextmanager
def _s3_world():
    endpoint = _required("AAP_REFERENCE_IO_S3_ENDPOINT")
    bucket = "reference-live-" + uuid4().hex
    config = {
        "endpoint": endpoint,
        "bucket": bucket,
        "base_prefix": "root",
        "region": "us-east-1",
    }
    secret = {
        "access_key": _required("AAP_REFERENCE_IO_S3_ACCESS_KEY"),
        "secret_key": _required("AAP_REFERENCE_IO_S3_SECRET_KEY"),
    }
    with closing(
        boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id=secret["access_key"],
            aws_secret_access_key=secret["secret_key"],
            config=Config(
                s3={"addressing_style": "path"},
                proxies={},
                retries={"total_max_attempts": 1},
            ),
        )
    ) as client:
        client.create_bucket(Bucket=bucket)
        try:
            client.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
            key = "root/collection/branch/edge +%2F.png"
            client.put_object(Bucket=bucket, Key=key, Body=_png("navy"))
            client.put_object(
                Bucket=bucket, Key="root/collection/skip.txt", Body=b"not an image"
            )
            client.put_object(
                Bucket=bucket,
                Key="root/collection-adjacent/other.png",
                Body=b"not in scope",
            )
            yield config, secret, client, key
        finally:
            for page in client.get_paginator("list_object_versions").paginate(
                Bucket=bucket
            ):
                for obj in page.get("Versions", []) + page.get("DeleteMarkers", []):
                    client.delete_object(
                        Bucket=bucket, Key=obj["Key"], VersionId=obj["VersionId"]
                    )
            client.delete_bucket(Bucket=bucket)


async def _denied(engine, sql):
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError) as denied:
            await conn.execute(text(sql))
        assert denied.value.orig.sqlstate == "42501"


async def _owner_world(owner, kind, config, secret, encryption_key):
    async with owner.begin() as db:
        actor, token = await _create_user(
            db, "project_admin", f"{uuid4().hex}@reference.test", "Reference owner"
        )
        other, other_token = await _create_user(
            db, "project_admin", f"{uuid4().hex}@reference.test", "Unrelated manager"
        )
        connection = StorageConnection(
            name=f"Reference {kind}",
            kind=kind,
            scope="owner",
            created_by=actor.id,
            config=config,
            secret_enc=encrypt_secret(secret, encryption_key),
        )
        dataset = Dataset(
            display_id=uuid4().hex[:15],
            name=f"Reference {uuid4().hex}",
            created_by=actor.id,
        )
        db.add_all([connection, dataset])
        await db.flush()
        location = await locations.register_connection(db, connection)
        return actor, token, other, other_token, location.id, dataset.id


def _identity(dsn):
    url = make_url(dsn)
    if not (url.database or "").endswith("_test"):
        pytest.fail("Reference acceptance DSN must target a disposable test database")
    return url.set(drivername="postgresql+asyncpg")


async def _scan(db, source_id, actor):
    run, created = await scans.start_scan(db, source_id, actor)
    assert created
    task_id = str(
        await db.scalar(
            text("SELECT celery_task_id FROM yoao_jobs_v1.read_job(:job_id)"),
            {"job_id": run.async_job_id},
        )
    )
    await db.commit()
    await execute_scan(run.id, task_id, _NODE)
    report = await read_scan(db, run.id)
    assert report.status == "completed", report.error_code
    assert report.complete
    await db.commit()
    return run.id


@pytest.mark.parametrize("kind", ("s3", "sftp"))
async def test_registered_reference_cutover(
    kind, test_engine, app_module, monkeypatch, tmp_path
):
    ordinary_url = _identity(
        _required("AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL")
    )
    source_url = _identity(_required("AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"))
    management_url = _identity(_required("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
    assert (
        ordinary_url.database
        == source_url.database
        == management_url.database
        == test_engine.url.database
    )
    assert ordinary_url.username == "identity_embedded_api_consumer"
    assert (
        len(
            {
                ordinary_url.username,
                source_url.username,
                management_url.username,
                test_engine.url.username,
            }
        )
        == 4
    )
    ordinary_engine = create_async_engine(ordinary_url, hide_parameters=True)
    source_engine = create_async_engine(source_url, hide_parameters=True)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    ordinary = async_sessionmaker(ordinary_engine, expire_on_commit=False)

    async def api_db():
        async with ordinary() as db:
            yield db

    app_module.dependency_overrides[get_db] = api_db
    monkeypatch.setattr(
        settings, "database_url", ordinary_url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    encryption_key = Fernet.generate_key().decode("ascii")
    try:
        await _denied(
            ordinary_engine, "SELECT secret_enc FROM public.storage_connections LIMIT 0"
        )
        await _denied(
            ordinary_engine,
            "SELECT * FROM yoao_assets_v1.reference_io_requests LIMIT 0",
        )
        await _denied(
            ordinary_engine,
            "SELECT yoao_assets_v1.capture_reference_io(NULL::uuid, 'probe')",
        )
        await _denied(
            source_engine, "SELECT secret_enc FROM public.storage_connections LIMIT 0"
        )
        await _denied(
            source_engine, "SELECT * FROM public.dataset_scan_entries LIMIT 0"
        )
        await _denied(
            source_engine,
            "SELECT yoao_assets_v1.prepare_reference_io(NULL,NULL,NULL,NULL,NULL,NULL)",
        )
        with _s3_world() if kind == "s3" else _sftp_world(tmp_path) as fixture:
            config, secret = fixture[:2]
            if kind == "s3":
                upstream, exact_key = fixture[2:]
                roots = {}
                sample = exact_key.removeprefix("root/")
                allowlist = [urlsplit(config["endpoint"]).hostname]
            else:
                qualification, image = fixture[2:]
                roots = {"test": qualification}
                sample = f"collection/{image.name}"
                allowlist = [config["host"]]
            monkeypatch.setattr(
                settings,
                "storage_sftp_roots",
                {
                    name: SftpReferenceRoot.model_validate(root)
                    for name, root in roots.items()
                },
            )
            (
                actor,
                token,
                other,
                other_token,
                location_id,
                dataset_id,
            ) = await _owner_world(
                owner,
                kind,
                config,
                secret,
                encryption_key,
            )
            async with real_saved_io_source(
                sftp_roots=roots,
                allowlist=allowlist,
                encryption_key=encryption_key,
            ) as (origin, tokens):
                read_token, write_token = tokens["read"], tokens["write"]
                monkeypatch.setattr(settings, "assets_source_url", origin)
                monkeypatch.setattr(
                    settings, "assets_source_saved_io_read_token", read_token
                )
                client = SavedIOClient(origin, read_token)
                async with httpx.AsyncClient(
                    base_url=origin, trust_env=False
                ) as internal:
                    for payload, token_used, status in (
                        ({"permit_id": str(uuid4())}, read_token, 403),
                        ({"permit_id": str(uuid4()), "path": sample}, read_token, 400),
                        ({"permit_id": str(uuid4())}, write_token, 401),
                    ):
                        refused = await internal.post(
                            "/v1/saved-io/reference/probe",
                            json=payload,
                            headers={"Authorization": f"Bearer {token_used}"},
                        )
                        assert refused.status_code == status
                auth = {"Authorization": f"Bearer {token}"}
                other_auth = {"Authorization": f"Bearer {other_token}"}
                location_path = f"/api/v1/storage-locations/{location_id}"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_module),
                    base_url="http://api.test",
                ) as api:
                    response = await api.post(
                        location_path + "/probe",
                        json={"sample_file": sample},
                        headers=auth,
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["capabilities"]["read"] == "verified"
                    assert (
                        await api.get(
                            location_path + "/directories", headers=other_auth
                        )
                    ).status_code == 404
                    page = await api.get(
                        location_path + "/directories",
                        params={"path": "collection", "limit": 1},
                        headers=auth,
                    )
                    assert page.status_code == 200, page.text
                    assert page.json()["entries"][0]["name"].rstrip("/") == "branch"
                    if kind == "sftp":
                        cursor = page.json()["next_cursor"]
                        assert cursor
                        next_page = await api.get(
                            location_path + "/directories",
                            params={"path": "collection", "cursor": cursor},
                            headers=auth,
                        )
                        assert [
                            item["name"] for item in next_page.json()["entries"]
                        ] == ["other"]
                        stale = await api.get(
                            location_path + "/directories",
                            params={"path": "", "cursor": cursor},
                            headers=auth,
                        )
                        assert stale.status_code == 409
                    async with ordinary() as db:
                        location = await get_location(db, location_id)
                        result = await locations.perform_probe(
                            db, location, f"worker:{_NODE}", sample, actor_id=actor.id
                        )
                        assert result["ok"], result["code"]
                        await locations.store_probe(
                            db, location, f"worker:{_NODE}", result, await scans.now(db)
                        )
                        await db.commit()
                        source = await scans.create_source(
                            db,
                            DatasetSourceCreate(
                                dataset_id=dataset_id,
                                location_id=location_id,
                                name=f"Reference {kind}",
                                relative_path="collection",
                                node_id=_NODE,
                                include_globs=["*.png"],
                                cache_policy="none",
                            ),
                            actor,
                        )
                        await db.commit()
                        run_id = await _scan(db, source.id, actor)
                        config_info = await reference_storage.configuration(
                            db, location
                        )
                        async with await storage_access.acquire(
                            db, location_id, "browse", node_id=locations.api_node_id()
                        ) as permit:
                            with pytest.raises(
                                ValueError, match="configuration_changed"
                            ):
                                await config_info._prepare(
                                    db,
                                    permit,
                                    "browse",
                                    other.id,
                                    None,
                                    None,
                                    {"path": "", "limit": 1},
                                )
                    digest = scans.path_digest(sample.removeprefix("collection/"))
                    rows = await api.get(
                        f"/api/v1/dataset-scans/{run_id}/entries", headers=auth
                    )
                    assert rows.status_code == 200, rows.text
                    entries = rows.json()["items"]
                    assert any(
                        row["relative_path"] == "skip.txt" and row["state"] == "skipped"
                        for row in entries
                    )
                    assert all(
                        "other.png" not in row["relative_path"] for row in entries
                    )
                    entry = f"/api/v1/dataset-scans/{run_id}/entries/{digest}"
                    verified = await api.post(entry + "/verify", headers=auth)
                    assert verified.status_code == 200, verified.text
                    sha = hashlib.sha256(_png("navy")).hexdigest()
                    assert verified.json()["sha256"] == sha
                    sampled = await api.get(
                        entry + "/content", params={"sha256": sha}, headers=auth
                    )
                    assert sampled.content == _png("navy")
                    registered = await api.post(entry + "/register", headers=auth)
                    assert registered.status_code == 200, registered.text
                    item_id = UUID(registered.json()["item_id"])
                    if kind == "s3":
                        upstream.put_object(
                            Bucket=config["bucket"], Key=exact_key, Body=_png("red")
                        )
                    async with ordinary() as db:
                        context = await reference_source_context(db, item_id, actor)
                        access = context["config"]
                        async with await storage_access.acquire(
                            db,
                            location_id,
                            "reference_read",
                            node_id=locations.api_node_id(),
                        ) as permit:
                            request = {
                                **reference_storage.original_request(
                                    context, None, "datasets:read"
                                ),
                                "offset": 2,
                                "length": 11,
                            }
                            subject = await access._prepare(
                                db, permit, "original", actor.id, None, None, request
                            )

                            def ranged():
                                with client.download(
                                    "reference", "original", subject
                                ) as stream:
                                    assert stream.metadata["sha256"] == sha
                                    assert stream.metadata["offset"] == 2
                                    return stream.read()

                            assert await asyncio.to_thread(ranged) == _png("navy")[2:13]
                            with pytest.raises(SavedIOError) as replay:
                                await asyncio.to_thread(ranged)
                            assert replay.value.status_code == 403
                        if kind == "sftp":
                            stamp = image.stat().st_mtime
                            image.write_bytes(_png("red"))
                            os.utime(image, (stamp + 30, stamp + 30))
                        assert await _scan(db, source.id, actor) != run_id
                    read_path = f"/api/v1/dataset-items/{item_id}/content"
                    output = await api.get(
                        read_path, params={"sha256": sha}, headers=auth
                    )
                    if kind == "s3":
                        assert output.status_code == 200 and output.content == _png(
                            "navy"
                        )
                    else:
                        assert (
                            output.status_code == 409
                            and output.json()["detail"]["code"] == "source_changed"
                        )
                        link = await api.post(
                            location_path + "/probe",
                            json={"sample_file": "collection/outside.png"},
                            headers=auth,
                        )
                        assert (
                            link.status_code == 200
                            and link.json()["code"] == "symlink_rejected"
                        )
                    unsupported_range = await api.get(
                        read_path,
                        params={"sha256": sha},
                        headers={**auth, "Range": "bytes=2-12"},
                    )
                    assert unsupported_range.status_code == 416
            if kind == "sftp":
                wrong_root = {
                    **qualification,
                    "host_key": _ssh_host_key(paramiko.RSAKey.generate(2048)),
                }
                monkeypatch.setattr(
                    settings,
                    "storage_sftp_roots",
                    {"test": SftpReferenceRoot.model_validate(wrong_root)},
                )
                async with real_saved_io_source(
                    sftp_roots={"test": wrong_root},
                    allowlist=allowlist,
                    encryption_key=encryption_key,
                ) as (origin, tokens):
                    monkeypatch.setattr(settings, "assets_source_url", origin)
                    monkeypatch.setattr(
                        settings, "assets_source_saved_io_read_token", tokens["read"]
                    )
                    async with ordinary() as db:
                        location = await get_location(db, location_id)
                        wrong = await locations.perform_probe(
                            db,
                            location,
                            locations.api_node_id(),
                            None,
                            actor_id=actor.id,
                        )
                        assert (
                            not wrong["ok"]
                            and wrong["code"] == "sftp_host_key_mismatch"
                        )
    finally:
        app_module.dependency_overrides.pop(get_db, None)
        await source_engine.dispose()
        await ordinary_engine.dispose()
