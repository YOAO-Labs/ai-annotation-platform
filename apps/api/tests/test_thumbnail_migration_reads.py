"""Exact published thumbnail reads, without database or provider resources."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import directory_previews as previews
from app.services import media_cache_storage, media_cache_urls, original_sources
from app.services import storage_access, storage_source_diagnostics as diagnostics
from app.services import storage_migration_artifact_reads as reads
from app.services import storage_migration_read_publications as publications
from app.services import thumbnail_outputs as outputs
from app.services.content_snapshot import ContentError
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_original_read_plan import source
from tests.test_storage_migration_artifact_reads import SDK, Target, route


def saved(version="source-version"):
    selected = route()
    identifier = uuid.uuid4()
    reference = {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(selected.source_location_id),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": selected.source_endpoint_url,
        "bucket": selected.source_bucket,
        "key_prefix": "tenant/_aap_managed/media-cache/",
        "prefix": "tenant/_aap_managed/media-cache/" + outputs.prefix_for(identifier),
        "storage_identity": managed_storage_identity(
            selected.source_endpoint_url, selected.source_bucket
        ),
    }
    row = SimpleNamespace(
        id=identifier,
        storage_ref=reference,
        version_id=version,
        etag=selected.source_etag,
        sha256=hashlib.sha256(b"data").hexdigest(),
        size_bytes=4,
        state="ready",
        uncertain_write=False,
        owner_kind="dataset_thumbnail",
        owner_id=uuid.UUID(int=11),
        input_ref={"source": outputs.source_facts(source())},
    )
    selected = replace(
        selected,
        source_object_key=outputs.owner(row).key(outputs.RELATIVE_KEY),
        source_version_id=version,
        sha256=row.sha256,
    )
    return row, selected


class Session:
    def __init__(self, row):
        self.row, self.gets = row, []
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, model, identifier, **kwargs):
        self.gets.append(kwargs)
        return self.row


@asynccontextmanager
async def locked_reads(db, lock):
    assert lock is True
    yield


def bind_session(monkeypatch, row):
    session = Session(row)
    monkeypatch.setattr(storage_access, "engine", lambda db: object())
    monkeypatch.setattr(outputs, "async_sessionmaker", lambda *a, **kw: lambda: session)
    monkeypatch.setattr(original_sources, "_reads", locked_reads)
    return session


def metadata(monkeypatch, selected):
    lookup = AsyncMock(return_value=selected)
    target = AsyncMock(side_effect=ContentError("storage_access_stopped"))
    monkeypatch.setattr(publications, "lookup", lookup)
    monkeypatch.setattr(media_cache_storage, "target_for_ref", target)
    return lookup, target


@pytest.mark.parametrize("version", [None, "null", "source-version"])
async def test_qualification_keeps_exact_source_receipt_and_unknown_version_mode(
    monkeypatch, version
):
    row, selected = saved(version)
    lookup, target = metadata(monkeypatch, selected)
    before = deepcopy(vars(row))
    assert await outputs.qualify_read("db", row) == (selected, None)
    lookup.assert_awaited_once_with(
        "db",
        source_location_id=selected.source_location_id,
        endpoint_url=selected.source_endpoint_url,
        bucket=selected.source_bucket,
        object_key=selected.source_object_key,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        source_version_id=publications.ANY_VERSION if version is None else version,
        source_etag=row.etag,
    )
    target.assert_not_awaited()
    assert vars(row) == before


async def test_absent_mapping_preserves_a_target_and_gate(monkeypatch):
    row, _ = saved()
    metadata(monkeypatch, None)
    target, check = AsyncMock(), AsyncMock(return_value=7)
    monkeypatch.setattr(media_cache_storage, "target_for_ref", target)
    monkeypatch.setattr(storage_access, "check", check)
    assert await outputs.qualify_read("db", row) == (None, 7)
    target.assert_awaited_once_with("db", row.storage_ref)
    check.assert_awaited_once_with("db", outputs.owner(row).location_id)
    check.side_effect = ContentError("storage_access_stopped")
    with pytest.raises(ContentError, match="storage_access_stopped"):
        await outputs.qualify_read("db", row)


async def test_route_control_failure_is_not_retried_at_a(monkeypatch):
    row, _ = saved()
    lookup, target = metadata(monkeypatch, None)
    lookup.side_effect = ContentError("read_publication_target_changed")
    with pytest.raises(ContentError, match="read_publication_target_changed"):
        await outputs.qualify_read("db", row)
    target.assert_not_awaited()


@pytest.mark.parametrize("version", [None, "null", "source-version"])
def test_physical_read_keeps_b_headers_and_saved_a_receipt(version):
    row, selected = saved(version)
    before = deepcopy(vars(row))
    sdk = SDK(selected)
    with reads.client(selected, Target(sdk)) as client:
        assert outputs.read_from_client(client, row) == b"data"
    assert sdk.calls == [
        (
            "get",
            {
                "Bucket": selected.target_bucket,
                "Key": selected.target_object_key,
                "VersionId": selected.target_version_id,
                "IfMatch": selected.target_etag,
            },
        )
    ]
    assert sdk.bodies[0].closed and sdk.closes == 1
    assert vars(row) == before


@pytest.mark.parametrize(
    "damage", ["bytes", "version", "etag", "body-close", "client-close"]
)
def test_published_read_rejects_corruption_and_preserves_close_responsibility(damage):
    row, selected = saved()
    sdk = SDK(selected)
    if damage == "bytes":
        row.sha256 = "f" * 64
    elif damage in {"version", "etag"}:
        sdk.changes["VersionId" if damage == "version" else "ETag"] = "foreign"
    elif damage == "body-close":
        sdk.body_close_failure = True
    else:
        sdk.close_failure = True
    with pytest.raises((ContentError, RuntimeError)) as error:
        with reads.client(selected, Target(sdk)) as client:
            outputs.read_from_client(client, row)
    if damage.endswith("close"):
        assert error.value.storage_operation_unsettled
    assert sdk.bodies[0].closes >= 1 and sdk.closes == 1


async def test_read_bytes_admits_b_owner_purpose_and_rechecks_before_delivery(
    monkeypatch,
):
    row, selected = saved()
    session = bind_session(monkeypatch, row)
    metadata(monkeypatch, selected)
    sdk, admissions = SDK(selected), []
    monkeypatch.setattr(
        publications, "target_for_route", AsyncMock(return_value=Target(sdk))
    )
    recheck = AsyncMock()
    monkeypatch.setattr(publications, "recheck", recheck)

    async def acquire(db, location_id, purpose, **kwargs):
        admissions.append((location_id, purpose, kwargs))

        @asynccontextmanager
        async def held():
            yield SimpleNamespace(revision=selected.maintenance_revision)

        return held()

    async def run(db, location_id, purpose, callback, **kwargs):
        admissions.append((location_id, purpose, kwargs["expected_revision"]))
        await kwargs["on_acquired"](
            SimpleNamespace(revision=selected.maintenance_revision)
        )
        return callback()

    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(storage_access, "run", run)
    assert await outputs.read_bytes(session, row.id) == b"data"
    assert admissions == [
        (
            selected.target_location_id,
            selected.purpose,
            {"expected_revision": selected.maintenance_revision},
        ),
        (
            selected.target_location_id,
            outputs.purpose(row.id),
            selected.maintenance_revision,
        ),
    ]
    assert session.gets[-1]["with_for_update"] == {"read": True, "nowait": True}
    assert recheck.await_count == 3
    recheck.assert_any_await(session, selected, lock=True)
    assert sdk.bodies[0].closed and sdk.closes == 1

    async def withdrawn(db, actual, **kwargs):
        if sdk.closes == 2:
            raise ContentError("read_publication_changed")

    monkeypatch.setattr(publications, "recheck", withdrawn)
    with pytest.raises(ContentError, match="read_publication_changed"):
        await outputs.read_bytes(session, row.id)
    assert sdk.bodies[-1].closed and sdk.closes == 2


@pytest.mark.parametrize(
    "field,value",
    [("state", "retiring"), ("etag", "changed"), ("uncertain_write", True)],
)
async def test_owner_is_rechecked_after_permit_before_sdk_open(
    monkeypatch, field, value
):
    row, selected = saved()
    session = bind_session(monkeypatch, row)
    metadata(monkeypatch, selected)

    async def run(db, route, purpose, callback, **kwargs):
        setattr(row, field, value)
        await kwargs["on_acquired"](SimpleNamespace(revision=1))
        pytest.fail("changed owner reached physical read")

    monkeypatch.setattr(reads, "run", run)
    with pytest.raises(ContentError, match="thumbnail_output_changed"):
        await outputs.read_bytes(session, row.id)


@pytest.mark.parametrize("mapped", [True, False])
@pytest.mark.parametrize("published_source", [True, False])
async def test_ready_signing_uses_current_admission_and_ignores_saved_transport_facts(
    monkeypatch, mapped, published_source
):
    row, selected = saved()
    session = bind_session(monkeypatch, row)
    metadata(monkeypatch, selected if mapped else None)
    monkeypatch.setattr(media_cache_storage, "target_for_ref", AsyncMock())
    monkeypatch.setattr(storage_access, "check", AsyncMock(return_value=1))
    monkeypatch.setattr(
        outputs,
        "_business_pointer",
        AsyncMock(return_value=(row.owner_kind, row.owner_id, row.id)),
    )
    monkeypatch.setattr(
        outputs,
        "_source",
        AsyncMock(
            return_value=replace(
                source(selected if published_source else None), maintenance_revision=99
            )
        ),
    )
    monkeypatch.setattr(original_sources, "recheck_read_route", AsyncMock())
    recheck = AsyncMock()
    monkeypatch.setattr(publications, "recheck", recheck)
    read = AsyncMock(return_value=b"data")
    monkeypatch.setattr(outputs, "read_bytes", read)
    before = deepcopy(vars(row))
    url = await outputs.thumbnail_url(
        session, "dataset_item", row.owner_id, selected.source_object_key
    )
    assert url.startswith(f"/api/v1/media-cache/thumbnail/{row.id}/thumbnail.webp?")
    token = url.split("token=", 1)[1]
    capability = media_cache_urls.decode(
        token, kind="thumbnail", output_id=row.id, relative="thumbnail.webp"
    )
    assert capability.reference == media_cache_urls.reference_digest(row.storage_ref)
    assert capability.sha256 == row.sha256
    if mapped:
        recheck.assert_awaited_once_with(session, selected, lock=True)
    read.assert_awaited_once()
    assert vars(row) == before


@pytest.mark.parametrize(
    "changed",
    [
        "source",
        "location",
        "config",
        "pointer",
        "publication",
        "original_publication",
        "source_gate",
    ],
)
async def test_signing_keeps_business_and_publication_bookends(monkeypatch, changed):
    row, selected = saved()
    session = bind_session(monkeypatch, row)
    metadata(monkeypatch, selected)
    pointer = row.owner_kind, row.owner_id, row.id
    pointers = [pointer, None if changed == "pointer" else pointer]
    monkeypatch.setattr(outputs, "_business_pointer", AsyncMock(side_effect=pointers))
    sources = [
        source(selected) if changed == "original_publication" else source(),
        replace(source(), sha256="f" * 64)
        if changed == "source"
        else source(selected)
        if changed == "original_publication"
        else source(),
    ]
    if changed == "location":
        sources[1] = replace(source(), location_id=uuid.uuid4())
    elif changed == "config":
        sources[1] = replace(source(), location_revision=99)
    elif changed == "source_gate":
        sources[1] = ContentError("storage_maintenance")
    monkeypatch.setattr(outputs, "_source", AsyncMock(side_effect=sources))
    monkeypatch.setattr(
        original_sources,
        "recheck_read_route",
        AsyncMock(
            side_effect=[None, ContentError("read_publication_changed")]
            if changed == "original_publication"
            else None
        ),
    )
    monkeypatch.setattr(outputs, "read_bytes", AsyncMock(return_value=b"data"))
    monkeypatch.setattr(
        publications,
        "recheck",
        AsyncMock(
            side_effect=ContentError("read_publication_changed")
            if changed == "publication"
            else None
        ),
    )
    sign = Mock()
    monkeypatch.setattr(media_cache_urls, "sign", sign)
    assert (
        await outputs.thumbnail_url(
            session, "dataset_item", row.owner_id, selected.source_object_key
        )
        is None
    )
    sign.assert_not_called()


def directory(row):
    row.owner_kind, row.owner_id = "directory_preview", uuid.UUID(int=20)
    row.path_digest = "d" * 64
    source_sha = "e" * 64
    row.input_ref = {
        "directory": previews.directory_facts(
            row.owner_id, row.path_digest, source_sha
        ),
        "encoded": {"sha256": row.sha256, "size_bytes": row.size_bytes},
    }
    saved = outputs.owner(row)
    return SimpleNamespace(
        source_id=row.owner_id,
        path_digest=row.path_digest,
        source_sha256=source_sha,
        thumbnail_output_id=row.id,
        spec=previews.SPEC,
        state="ready",
        generation=1,
        location_id=saved.location_id,
        bucket=saved.bucket,
        object_key=saved.key(outputs.RELATIVE_KEY),
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        asset_id=uuid.UUID(int=30),
    )


async def test_directory_owner_qualification_uses_its_published_thumbnail(monkeypatch):
    row, selected = saved()
    preview = directory(row)
    db = Session(row)
    metadata(monkeypatch, selected)
    assert await previews.output_for_row(db, preview) is row
    preview.sha256 = "f" * 64
    with pytest.raises(ContentError, match="preview_binding_changed"):
        await previews.output_for_row(db, preview)


async def test_diagnostics_qualifies_each_saved_member_and_keeps_original_directory_gate(
    monkeypatch,
):
    first, selected = saved()
    second, _ = saved()
    a, b = directory(first), directory(second)
    external = uuid.UUID(int=90)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=[(a, first, external), (b, second, external)])
    )
    metadata(monkeypatch, selected)
    monkeypatch.setattr(publications, "lookup", AsyncMock(side_effect=[selected, None]))
    check = AsyncMock(return_value=1)
    monkeypatch.setattr(storage_access, "check", check)
    assert await diagnostics.available_preview_keys(db, [a.source_id]) == [
        (a.source_id, a.path_digest)
    ]
    check.assert_awaited_once_with(db, external)


async def test_reuse_does_not_treat_publication_control_failure_as_damaged_cache(
    monkeypatch,
):
    row, _ = saved()
    preview = directory(row)
    preview.policy_revision = 1
    source_row = SimpleNamespace(
        id=preview.source_id,
        location_id=uuid.UUID(int=90),
        config_revision=1,
        policy_revision=1,
    )
    location = SimpleNamespace(config_revision=1)
    db = SimpleNamespace(get=AsyncMock(side_effect=[location, preview]))
    monkeypatch.setattr(previews.policy, "effective_cache_policy", lambda *a: "preview")
    monkeypatch.setattr(previews.policy, "permits_preview", lambda *a: True)
    monkeypatch.setattr(
        previews,
        "output_for_row",
        AsyncMock(side_effect=ContentError("read_publication_changed")),
    )
    encode = AsyncMock()
    monkeypatch.setattr(previews, "encode", encode)
    with pytest.raises(ContentError, match="read_publication_changed"):
        await previews.prepare(
            db,
            source_row,
            SimpleNamespace(path_digest=preview.path_digest),
            SimpleNamespace(sha256=preview.source_sha256),
        )
    encode.assert_not_awaited()
