"""Selected derivative members preserve owners, read fences and physical closure."""

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import (
    image_pyramid_objects as images,
    image_pyramid_storage as image_storage,
    video_derivative_objects as videos,
    video_derivative_storage as video_storage,
    video_derivatives as derivatives,
    storage_access,
    storage_migration_artifact_reads as reads,
    storage_migration_copy as copy,
    storage_migration_read_publications as publications,
)
from app.services.content_snapshot import ContentError
from fastapi import HTTPException
from tests.test_image_pyramid_storage import reference as image_ref
from tests.test_video_derivative_storage import reference as video_ref
from tests.test_storage_migration_artifact_reads import route as base_route


def saved(family):
    identifier = uuid.uuid4()
    now = datetime.now(timezone.utc)
    if family == "image":
        storage, owners = image_storage, images
        row = SimpleNamespace(
            id=identifier,
            asset_id=uuid.uuid4(),
            generation=1,
            lease_token=uuid.uuid4(),
            state="ready",
            storage_ref=image_ref(identifier),
        )
        content = {
            "manifest.json": b'{"schema":"test"}',
            "overview.webp": b"overview",
            "tiles/0/0/0.webp": b"tile",
        }
    else:
        storage, owners = video_storage, videos
        row = SimpleNamespace(
            id=identifier,
            dataset_item_id=uuid.uuid4(),
            kind="metadata",
            spec={"schema": 1},
            original_ref={"source": "frozen"},
            auth_ref={"actor": "original"},
            metadata_output_id=None,
            storage_ref=video_ref(identifier),
            state="ready",
            lease_token=uuid.uuid4(),
            lease_expires_at=now,
            result={"schema": 1},
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        content = {
            "metadata.json": b'{"schema":1}',
            "poster.webp": b"poster",
            "playback.mp4": b"video-body",
        }
    owner = storage.resolve(row)
    objects = {owner.key(key): data for key, data in content.items()}
    row.inventory = {
        "schema": 1,
        "objects": {
            key: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            for key, data in objects.items()
        },
    }
    template = base_route()
    routes = {}
    for index, (key, data) in enumerate(objects.items()):
        route = replace(
            template,
            source_location_id=uuid.UUID(owner.ref["location_id"]),
            source_endpoint_url=owner.ref["endpoint_url"],
            source_bucket=owner.bucket,
            source_object_key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            source_object_id=f"{index + 1:064x}",
            target_object_key=f"_aap_migration/member-{index}",
            target_version_id=f"version-{index}",
        )
        routes[key] = replace(
            route,
            target_content_sha256=copy.digest(copy.content_target(metadata(route))),
        )
    return storage, owners, row, objects, routes


def metadata(route):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "ContentLength": route.size_bytes,
        "ETag": route.target_etag,
        "VersionId": route.target_version_id,
        "ContentType": "application/octet-stream",
        "Metadata": {
            "aap-copy-plan": route.creation_plan_uuid,
            "aap-copy-object": route.source_object_id,
            "aap-copy-nonce": route.creation_nonce,
        },
    }


class Provider:
    def __init__(self, objects, routes):
        self.objects, self.routes = objects, routes
        self.meta = SimpleNamespace(
            endpoint_url=next(iter(routes.values())).target_endpoint_url
        )
        self.calls, self.bodies = [], []
        self.closed = False
        self.close_failure = False
        self.corrupt = False

    def select(self, params):
        route = next(
            route
            for route in self.routes.values()
            if route.target_object_key == params["Key"]
        )
        assert params["Bucket"] == route.target_bucket
        assert params["VersionId"] == route.target_version_id
        return route

    def head_object(self, **params):
        self.calls.append(("head", params))
        return metadata(self.select(params))

    def get_object(self, **params):
        self.calls.append(("get", params))
        route = self.select(params)
        assert params["IfMatch"] == route.target_etag
        data = self.objects[route.source_object_key]
        body = io.BytesIO(b"X" * len(data) if self.corrupt else data)
        self.bodies.append(body)
        return {**metadata(route), "Body": body}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        assert operation == "get_object"
        self.select(Params)
        self.calls.append(("sign", {**Params, "ExpiresIn": ExpiresIn}))
        return "https://target.test/exact-version"

    def close(self):
        if self.close_failure:
            raise storage_access.mark_unsettled(RuntimeError("client close unknown"))
        self.closed = True


class Target:
    def __init__(self, provider):
        self.provider = provider

    @contextmanager
    def client(self):
        try:
            yield self.provider
        finally:
            storage_access.close_registered(self.provider)


@pytest.mark.parametrize("family", ["image", "video"])
async def test_plan_only_resolves_selected_members_and_rechecks_old_routes(
    monkeypatch, family
):
    storage, owners, row, _, routes = saved(family)
    key = storage.resolve(row).key(
        "manifest.json" if family == "image" else "metadata.json"
    )
    resolver = AsyncMock(side_effect=lambda _db, _ref, key, *_facts: routes[key])
    recheck = AsyncMock()
    gate = AsyncMock(side_effect=AssertionError("A remains stopped"))
    monkeypatch.setattr(reads, "resolve", resolver)
    monkeypatch.setattr(publications, "recheck", recheck)
    monkeypatch.setattr(storage_access, "check", gate)
    db = object()
    plan = await owners.read_plan(db, row, [key, key])
    assert len(plan.members) == 1 and plan.members[0][0] == key
    assert resolver.await_args.args[2:] == (
        key,
        row.inventory["objects"][key]["sha256"],
        len((b'{"schema":"test"}' if family == "image" else b'{"schema":1}')),
    )
    recheck.reset_mock()
    recheck.side_effect = ContentError("read_publication_changed")
    with pytest.raises(ContentError, match="read_publication_changed"):
        await owners.check_reads(db, row, plan, lock=True)
    assert resolver.await_count == 1
    recheck.assert_awaited_once_with(db, routes[key], lock=True)
    gate.assert_not_awaited()


@pytest.mark.parametrize("family", ["image", "video"])
async def test_partial_selected_set_keeps_unmapped_source_gate_and_ready_only(
    monkeypatch, family
):
    _, owners, row, _, routes = saved(family)
    keys = list(routes)[:2]
    monkeypatch.setattr(
        reads, "resolve", AsyncMock(side_effect=[routes[keys[0]], None])
    )
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    gate = AsyncMock(side_effect=ContentError("storage_maintenance"))
    monkeypatch.setattr(storage_access, "check", gate)
    with pytest.raises(ContentError, match="storage_maintenance"):
        await owners.read_plan(object(), row, keys)
    gate.assert_awaited_once()
    row.state = "building"
    with pytest.raises(ContentError):
        await owners.read_plan(object(), row, keys)


@pytest.mark.parametrize("family", ["image", "video"])
@pytest.mark.parametrize("fault", [None, "hash", "foreign"])
def test_read_storage_hashes_and_signs_exact_b_without_changing_source(family, fault):
    storage, _, row, objects, routes = saved(family)
    key = next(iter(routes))
    selected = routes[key]
    before = deepcopy(row.storage_ref)
    provider = Provider(objects, routes)
    if fault == "hash":
        provider.corrupt = True
    if fault == "foreign":
        for member_key, route in routes.items():
            routes[member_key] = replace(
                route, source_endpoint_url="http://foreign.test"
            )
    with reads.clients(tuple(routes.values()), Target(provider)) as client:
        members = tuple(
            (key, facts["sha256"], facts["size_bytes"], routes[key])
            for key, facts in row.inventory["objects"].items()
        )
        transport = storage.ReadStorage(
            deepcopy(row.storage_ref), members, None, client
        )
        operation = storage.verify_object if family == "image" else storage.verify_file
        if fault is not None:
            with pytest.raises(ValueError):
                operation(transport, row, key)
        else:
            operation(transport, row, key)
            assert (
                storage.signed_url(transport, row, key, 123)
                == "https://target.test/exact-version"
            )
            assert provider.calls[-1][1]["ExpiresIn"] == 123
            assert provider.calls[-1][1]["VersionId"] == selected.target_version_id
            if family == "image":
                assert storage.read_manifest(
                    transport, row, "sha256:" + selected.sha256
                ) == {"schema": "test"}
            else:
                assert json.loads(storage.read_bytes(transport, row, key)) == {
                    "schema": 1
                }
        assert row.storage_ref == before
    assert provider.closed and all(body.closed for body in provider.bodies)
    assert not any(
        operation in {"list", "put", "delete"} for operation, _ in provider.calls
    )


@pytest.mark.parametrize("family", ["image", "video"])
@pytest.mark.parametrize("close_failure", [False, True])
async def test_access_closes_before_final_guard_and_retains_unknown_responsibility(
    monkeypatch, family, close_failure
):
    storage, owners, row, objects, routes = saved(family)
    receipt = owners.snapshot(row)
    plan = owners.ReadPlan(
        receipt,
        tuple(
            (key, facts["sha256"], facts["size_bytes"], routes[key])
            for key, facts in row.inventory["objects"].items()
        ),
        None,
    )
    provider = Provider(objects, routes)
    provider.close_failure = close_failure
    session = SimpleNamespace(commit=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(storage_access, "engine", Mock(return_value=object()))
    monkeypatch.setattr(
        owners, "async_sessionmaker", lambda *_args, **_kwargs: lambda: session
    )
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    monkeypatch.setattr(
        storage_access, "check", AsyncMock(side_effect=AssertionError("source Gate"))
    )
    active, failures, guards = [], [], []

    @asynccontextmanager
    async def admit(db, route, purpose):
        assert purpose == owners.purpose(receipt.id)
        active.append(True)
        try:
            yield SimpleNamespace(target=Target(provider))
        except BaseException as exc:
            failures.append(exc)
            raise
        finally:
            active.pop()

    async def guard(_revision):
        assert active
        guards.append(provider.closed)

    monkeypatch.setattr(reads, "admit", admit)

    async def run():
        async with owners.read_access(object(), receipt, plan, guard) as transport:
            key = next(iter(routes))
            await owners.originals.physical(
                storage.verify_object if family == "image" else storage.verify_file,
                transport,
                receipt,
                key,
            )

    if close_failure:
        with pytest.raises(RuntimeError):
            await run()
        assert failures[0].storage_operation_unsettled is True
        assert guards == [False]
        provider.close_failure = False
        provider.close()
    else:
        await run()
        assert guards == [False, True]
    assert not active and all(body.closed for body in provider.bodies)
    session.close.assert_awaited_once()


async def test_video_consumed_set_is_scoped_and_cannot_refresh_withdrawn_members(
    monkeypatch,
):
    _, _, row, _, routes = saved("video")
    receipt = videos.snapshot(row)
    members = tuple(
        (key, facts["sha256"], facts["size_bytes"], routes[key])
        for key, facts in row.inventory["objects"].items()
    )
    first, later = (
        videos.ReadPlan(receipt, members[:1], None),
        videos.ReadPlan(receipt, members[1:2], None),
    )
    db, authority = SimpleNamespace(info={}), object()
    videos.begin_reads(db, authority)
    videos.remember_reads(db, receipt, authority, first)
    videos.remember_reads(db, receipt, authority, later)
    consumed = videos.consumed(db, receipt, authority)
    assert len(consumed.members) == 2
    monkeypatch.setattr(
        publications,
        "recheck",
        AsyncMock(side_effect=ContentError("read_publication_changed")),
    )
    resolver = AsyncMock()
    monkeypatch.setattr(reads, "resolve", resolver)
    with pytest.raises(ContentError, match="read_publication_changed"):
        await videos.check_reads(db, receipt, consumed, lock=True)
    resolver.assert_not_awaited()
    assert videos.consumed(db, receipt, object()) is None
    videos.begin_reads(db, authority)
    assert videos.consumed(db, receipt, authority) is None
    changed = replace(receipt, lease_token=uuid.uuid4())
    videos.remember_reads(db, receipt, authority, first)
    with pytest.raises(ContentError):
        videos.consumed(db, changed, authority)


async def test_video_outer_ready_check_uses_metadata_route_and_final_consumed_set(
    monkeypatch,
):
    storage, _, row, _, routes = saved("video")
    source, authority = SimpleNamespace(item_id=row.dataset_item_id), object()
    item = SimpleNamespace(video_output_id=row.id)
    db = SimpleNamespace(info={})
    monkeypatch.setattr(derivatives.originals, "check", AsyncMock(return_value=source))
    monkeypatch.setattr(
        derivatives.originals, "receipt", lambda _source: row.original_ref
    )
    monkeypatch.setattr(derivatives.originals, "clock", Mock())
    monkeypatch.setattr(
        derivatives, "row", AsyncMock(side_effect=[item, row, item, row])
    )
    monkeypatch.setattr(derivatives, "_slot", AsyncMock(return_value=item))
    monkeypatch.setattr(
        reads,
        "resolve",
        AsyncMock(side_effect=lambda _db, _ref, key, *_facts: routes[key]),
    )
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    source_gate = AsyncMock(side_effect=AssertionError("A remains stopped"))
    monkeypatch.setattr(storage_access, "check", source_gate)
    await derivatives.check(db, row, authority)
    assert reads.resolve.await_count == 1
    assert reads.resolve.await_args.args[2] == storage.resolve(row).key("metadata.json")
    plan = await videos.read_plan(db, row, list(routes)[:2])
    videos.begin_reads(db, authority)
    videos.remember_reads(db, row, authority, plan)
    reads.resolve.reset_mock()
    publications.recheck.side_effect = ContentError("read_publication_changed")
    with pytest.raises(ContentError, match="read_publication_changed"):
        await derivatives.check(db, row, authority)
    reads.resolve.assert_not_awaited()
    source_gate.assert_not_awaited()


@pytest.mark.parametrize("family", ["image", "video"])
async def test_mixed_members_keep_separate_source_and_target_admission(
    monkeypatch, family
):
    storage, owners, row, objects, routes = saved(family)
    keys = list(objects)[:2]
    source_key, target_key = keys
    facts = row.inventory["objects"]
    receipt = owners.snapshot(row)
    plan = owners.ReadPlan(
        receipt,
        (
            (
                source_key,
                facts[source_key]["sha256"],
                facts[source_key]["size_bytes"],
                None,
            ),
            (
                target_key,
                facts[target_key]["sha256"],
                facts[target_key]["size_bytes"],
                routes[target_key],
            ),
        ),
        5,
    )
    provider = Provider(objects, routes)
    active = []
    session = SimpleNamespace(commit=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(storage_access, "engine", Mock(return_value=object()))
    monkeypatch.setattr(
        owners, "async_sessionmaker", lambda *_args, **_kwargs: lambda: session
    )
    monkeypatch.setattr(publications, "recheck", AsyncMock())
    gate = AsyncMock(return_value=5)
    monkeypatch.setattr(storage_access, "check", gate)
    monkeypatch.setattr(storage, "transport_target", AsyncMock(return_value=None))

    def source_head(**params):
        assert active == ["A", "B"]
        assert params == {"Bucket": row.storage_ref["bucket"], "Key": source_key}
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": facts[source_key]["size_bytes"],
            "ETag": '"source-etag"',
        }

    source_bodies = []

    def source_get(**params):
        assert params.pop("IfMatch") == '"source-etag"'
        result = source_head(**params)
        body = io.BytesIO(objects[source_key])
        source_bodies.append(body)
        return {**result, "Body": body}

    source = SimpleNamespace(
        client=SimpleNamespace(
            meta=SimpleNamespace(endpoint_url=row.storage_ref["endpoint_url"]),
            head_object=source_head,
            get_object=source_get,
        )
    )

    @contextmanager
    def source_context():
        yield source

    if family == "image":
        monkeypatch.setattr(owners, "storage_service", source)
    else:
        monkeypatch.setattr(storage, "open_storage", source_context)

    @asynccontextmanager
    async def source_admission():
        active.append("A")
        try:
            yield object()
        finally:
            assert active.pop() == "A"

    async def acquire(db, location, purpose, *, expected_revision):
        assert location == uuid.UUID(row.storage_ref["location_id"])
        assert purpose == owners.purpose(row.id) and expected_revision == 5
        return source_admission()

    @asynccontextmanager
    async def target_admission(db, route, purpose):
        assert route is routes[target_key] and purpose == owners.purpose(row.id)
        active.append("B")
        try:
            yield SimpleNamespace(target=Target(provider))
        finally:
            assert active.pop() == "B"

    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(reads, "admit", target_admission)
    async with owners.read_access(object(), receipt, plan, AsyncMock()) as transport:
        for key in keys:
            await owners.originals.physical(
                storage.verify_object if family == "image" else storage.verify_file,
                transport,
                receipt,
                key,
            )
    assert not active and provider.closed
    assert all(body.closed for body in [*source_bodies, *provider.bodies])
    assert all(
        params["Key"] == routes[target_key].target_object_key
        for _, params in provider.calls
    )


@pytest.mark.parametrize("fault", [None, "coordinate", "generation"])
def test_pyramid_selection_keeps_requested_members_and_existing_errors(fault):
    from app.api.v1.tasks.image_pyramid import _requested_keys

    prefix = image_storage.prefix_for(uuid.uuid4())
    generation = SimpleNamespace(
        generation=1,
        width=1024,
        height=512,
        manifest_key=prefix + "manifest.json",
        overview_key=prefix + "overview.webp",
    )
    item = SimpleNamespace(
        kind="tile",
        generation=2 if fault == "generation" else 1,
        level=0,
        x=2 if fault == "coordinate" else 1,
        y=0,
    )
    payload = SimpleNamespace(items=[item, item])
    if fault:
        with pytest.raises(HTTPException) as rejected:
            _requested_keys(generation, payload)
        assert rejected.value.status_code == (422 if fault == "coordinate" else 409)
    else:
        assert _requested_keys(generation, payload) == (
            prefix + "manifest.json",
            prefix + "tiles/0/1/0.webp",
        )


@pytest.mark.parametrize(
    "field,value",
    [("width", None), ("height", 0), ("manifest_key", None), ("overview_key", 1)],
)
def test_pyramid_selection_rejects_inconsistent_saved_geometry_as_receipt_error(
    field, value
):
    from app.api.v1.tasks.image_pyramid import _requested_keys

    prefix = image_storage.prefix_for(uuid.uuid4())
    generation = SimpleNamespace(
        generation=1,
        width=1024,
        height=512,
        manifest_key=prefix + "manifest.json",
        overview_key=prefix + "overview.webp",
    )
    setattr(generation, field, value)
    payload = SimpleNamespace(items=[SimpleNamespace(kind="overview", generation=1)])
    with pytest.raises(image_storage.ImagePyramidStorageError) as rejected:
        _requested_keys(generation, payload)
    assert rejected.value.code == "image_pyramid_storage_invalid_receipt"
