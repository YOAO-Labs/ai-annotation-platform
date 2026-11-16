from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from yoao_assets_images import contract as images
from yoao_assets_images.contract import ImageFailure, NativePyramidSavedTarget
from yoao_assets_images.native_storage_contract import (
    NATIVE_READ_REFERENCES_PATH,
    NATIVE_WRITE_REFERENCE_PATH,
    NativePyramidIoContext,
    NativePyramidIoDestination,
    NativePyramidIoMember,
    NativePyramidIoReferences,
    NativePyramidReadIoRequest,
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    NativePyramidWriteIoRequest,
)
from yoao_assets_source_runtime.api import create_app
from yoao_assets_source_runtime.config import ConfigurationError, load_config
from yoao_assets_source_runtime.native_storage import NativeStorageSigner

_NATIVE_ENV = {
    "ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN": "w" * 40,
    "ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN": "r" * 40,
    "ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN": "c" * 40,
    "ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY": "native-access",
    "ASSETS_SOURCE_NATIVE_S3_SECRET_KEY": "native-secret",
}


def _config(**overrides):
    environment = {
        "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
        "ASSETS_SOURCE_TOKEN": "s" * 40,
        "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN": "v" * 40,
        "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
        "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
        "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
        "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
        "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
        "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
    }
    return load_config({**environment, **overrides})


def _saved_target(
    output_id,
    *,
    key_prefix="",
    bucket="media-cache",
    endpoint="http://minio.internal:9000",
):
    return NativePyramidSavedTarget.model_validate(
        {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": uuid4(),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": bucket,
            "key_prefix": key_prefix,
            "prefix": key_prefix
            + "image-pyramids/objects/"
            + str(output_id)
            + "/",
            "storage_identity": "a" * 64,
        }
    )


def _member(
    operation_id,
    *,
    relative_key="overview.webp",
    etag='"etag"',
    version_id="v1",
):
    return NativePyramidIoMember(
        operation_id=operation_id,
        relative_key=relative_key,
        sha256="b" * 64,
        size_bytes=12,
        etag=etag,
        version_id=version_id,
    )


def _context(
    mode,
    *,
    output_id,
    scope_id,
    scope_token,
    members,
    valid_until=None,
    url_expiry_seconds=900,
    destination=None,
    host_allowlist=None,
    storage_ref=None,
):
    if valid_until is None and mode != "read":
        valid_until = datetime.now(timezone.utc) + timedelta(seconds=120)
    return NativePyramidIoContext(
        mode=mode,
        scope_id=scope_id,
        scope_token=scope_token,
        output_id=output_id,
        storage_ref=storage_ref or _saved_target(output_id),
        valid_until=valid_until,
        url_expiry_seconds=url_expiry_seconds,
        members=members,
        destination=destination,
        host_allowlist=host_allowlist,
    )


class _Data:
    def __init__(self, *contexts):
        self._contexts = list(contexts)
        self.calls = []

    async def _next(self, *arguments):
        self.calls.append(arguments)
        context = (
            self._contexts.pop(0)
            if len(self._contexts) > 1
            else self._contexts[0]
        )
        return context

    async def capture_native_pyramid_write_io(self, *arguments):
        return await self._next(*arguments)

    async def capture_native_pyramid_read_io(self, *arguments):
        return await self._next(*arguments)

    async def capture_native_pyramid_retirement_io(self, *arguments):
        return await self._next(*arguments)


def _signer(*contexts):
    config = _config(**_NATIVE_ENV)
    return NativeStorageSigner(config, config.native, _Data(*contexts))


def _query(request: NativePyramidSignedRequest) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))


def _write_request(operation_id, lease_token):
    return NativePyramidWriteIoRequest(
        operation_id=operation_id,
        job_id=uuid4(),
        job_generation=1,
        lease_token=lease_token,
    )


async def test_put_reference_signs_only_the_exact_reserved_write():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id, etag=None, version_id=None)
    context = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
    )
    signer = _signer(context)
    result = await signer.write_reference(_write_request(operation_id, lease_token))
    assert result.mode == "put"
    assert result.expires_in is None
    signed = result.members[0]
    assert signed.member.operation_id == operation_id
    assert signed.put is not None
    assert signed.head is None and signed.get is None and signed.delete is None
    request = signed.put
    assert request.method == "PUT"
    assert request.headers == {
        "content-length": "12",
        "content-type": "image/webp",
        "if-none-match": "*",
        "x-amz-meta-sha256": "b" * 64,
    }
    query = _query(request)
    assert query["X-Amz-SignedHeaders"] == (
        "content-length;content-type;host;if-none-match;x-amz-meta-sha256"
    )
    assert "versionId" not in query
    assert urlsplit(request.url).path == (
        "/media-cache/" + context.storage_ref.prefix + "overview.webp"
    )
    assert request.expires_at <= context.valid_until


async def test_verify_reference_uses_the_recorded_receipt():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id)
    context = _context(
        "verify",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
    )
    result = await _signer(context).verify_reference(
        _write_request(operation_id, lease_token)
    )
    signed = result.members[0]
    assert signed.get is not None and signed.head is None
    assert signed.get.headers == {"if-match": '"etag"'}
    query = _query(signed.get)
    assert query["versionId"] == "v1"
    assert query["X-Amz-SignedHeaders"] == "host;if-match"


async def test_saved_null_version_is_signed_exactly_and_none_is_absent():
    for saved_version, expected in (("null", "null"), (None, None)):
        output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
        member = _member(operation_id, version_id=saved_version)
        context = _context(
            "verify",
            output_id=output_id,
            scope_id=operation_id,
            scope_token=lease_token,
            members=[member],
        )
        result = await _signer(context).verify_reference(
            _write_request(operation_id, lease_token)
        )
        query = _query(result.members[0].get)
        if expected is None:
            assert "versionId" not in query
        else:
            assert query["versionId"] == expected


async def test_read_inspection_and_publication_use_the_frozen_policy():
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    member = _member(uuid4())
    context = _context(
        "read",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=token,
        members=[member],
        url_expiry_seconds=300,
        valid_until=None,
    )
    signer = _signer(context)
    inspect = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="inspect",
        )
    )
    assert inspect.expires_in is None
    assert inspect.members[0].public_url is None
    assert inspect.members[0].head is not None
    assert inspect.members[0].get is not None
    for signed in (inspect.members[0].head, inspect.members[0].get):
        assert signed.headers == {"if-match": '"etag"'}
        assert _query(signed)["versionId"] == "v1"

    publish = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="publish",
            expires_in=120,
        )
    )
    assert publish.expires_in == 120
    public = publish.members[0]
    assert public.public_url is not None
    assert public.head is None and public.get is None
    public_query = dict(
        parse_qsl(urlsplit(public.public_url).query, keep_blank_values=True)
    )
    assert public_query["X-Amz-Expires"] == "120"
    assert public_query["versionId"] == "v1"
    assert public.public_expires_at is not None
    assert urlsplit(public.public_url).hostname == "storage.example.test"

    defaulted = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="publish",
        )
    )
    assert defaulted.expires_in == 300


async def test_read_publish_rejects_an_explicit_lifetime_above_policy():
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    member = _member(uuid4())
    context = _context(
        "read",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=token,
        members=[member],
        url_expiry_seconds=300,
        valid_until=None,
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(context).read_references(
            NativePyramidReadIoRequest(
                operation_id=operation_id,
                token=token,
                relative_keys=[member.relative_key],
                purpose="publish",
                expires_in=301,
            )
        )
    assert caught.value.code == "invalid_request"


async def test_retirement_members_sign_conditional_exact_versions():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(uuid4())
    context = _context(
        "retirement_members",
        output_id=output_id,
        scope_id=page_id,
        scope_token=lease_token,
        members=[member],
    )
    result = await _signer(context).retirement_references(
        NativePyramidRetirementIoRequest(
            output_id=output_id, lease_token=lease_token, page_id=page_id
        )
    )
    signed = result.members[0]
    assert signed.put is None and signed.public_url is None
    for request, method in (
        (signed.head, "HEAD"),
        (signed.get, "GET"),
        (signed.delete, "DELETE"),
    ):
        assert request is not None
        assert request.method == method
        assert request.headers == {"if-match": '"etag"'}
        query = _query(request)
        assert query["versionId"] == "v1"
        assert query["X-Amz-SignedHeaders"] == "host;if-match"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("objects", {"list-type": "2", "max-keys": "1"}),
        ("versions", {"versions": "", "max-keys": "1"}),
        ("uploads", {"uploads": "", "max-uploads": "100"}),
    ],
)
async def test_retirement_prefix_signs_only_the_exact_absence_probe(kind, expected):
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    context = _context(
        "retirement_prefix",
        output_id=output_id,
        scope_id=page_id,
        scope_token=lease_token,
        members=[],
    )
    result = await _signer(context).retirement_prefix_reference(
        NativePyramidRetirementPrefixIoRequest(
            output_id=output_id,
            lease_token=lease_token,
            page_id=page_id,
            kind=kind,
        )
    )
    request = result.request
    assert request.method == "GET"
    assert request.headers == {}
    assert urlsplit(request.url).path == "/media-cache"
    query = _query(request)
    for name, value in expected.items():
        assert query[name] == value
    if kind == "uploads":
        assert "prefix" not in query
    else:
        assert query["prefix"] == context.storage_ref.prefix
    assert query["X-Amz-SignedHeaders"] == "host"
    assert set(query) - {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
        "prefix",
        *expected,
    } == set()


async def test_prefix_route_refuses_a_nonterminal_or_changed_scope():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(uuid4())
    context = _context(
        "retirement_members",
        output_id=output_id,
        scope_id=page_id,
        scope_token=lease_token,
        members=[member],
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(context).retirement_prefix_reference(
            NativePyramidRetirementPrefixIoRequest(
                output_id=output_id,
                lease_token=lease_token,
                page_id=page_id,
                kind="objects",
            )
        )
    assert caught.value.code == "invalid_response"

    changed = _context(
        "put",
        output_id=output_id,
        scope_id=page_id,
        scope_token=lease_token,
        members=[_member(page_id, etag=None, version_id=None)],
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(changed).write_reference(
            _write_request(uuid4(), lease_token)
        )
    assert caught.value.code == "invalid_response"


async def test_expired_lease_and_nonadjacent_scope_are_refused():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id)
    expired = _context(
        "verify",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(expired).verify_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "lease_lost"

    first = _context(
        "verify",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
    )
    second = first.model_copy(update={"scope_token": uuid4()})
    with pytest.raises(ImageFailure) as caught:
        await _signer(first, second).verify_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"


async def test_recapture_allows_a_heartbeat_extension_only():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id, etag=None, version_id=None)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=120)
    first = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        valid_until=deadline,
    )
    renewed = first.model_copy(
        update={"valid_until": deadline + timedelta(seconds=120)}
    )
    result = await _signer(first, renewed).write_reference(
        _write_request(operation_id, lease_token)
    )
    assert result.members[0].put.expires_at <= deadline

    shortened = first.model_copy(
        update={"valid_until": deadline - timedelta(seconds=60)}
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(first, shortened).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    cleared = first.model_copy(update={"valid_until": None})
    with pytest.raises(ImageFailure) as caught:
        await _signer(first, cleared).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    changed_policy = first.model_copy(update={"host_allowlist": []})
    with pytest.raises(ImageFailure) as caught:
        await _signer(first, changed_policy).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    receipt = _member(operation_id)
    verified = _context(
        "verify",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[receipt],
    )
    changed_receipt = verified.model_copy(
        update={
            "members": [receipt.model_copy(update={"sha256": "d" * 64})],
        }
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(verified, changed_receipt).verify_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    read_member = _member(uuid4())
    read_first = _context(
        "read",
        output_id=output_id,
        scope_id=uuid4(),
        scope_token=uuid4(),
        members=[read_member],
        valid_until=None,
    )
    read_second = read_first.model_copy(
        update={"valid_until": datetime.now(timezone.utc) + timedelta(seconds=60)}
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(read_first, read_second).read_references(
            NativePyramidReadIoRequest(
                operation_id=read_first.scope_id,
                token=read_first.scope_token,
                relative_keys=[read_member.relative_key],
                purpose="inspect",
            )
        )
    assert caught.value.code == "dependency_changed"


async def test_mutated_saved_target_and_host_policy_are_refused():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id, etag=None, version_id=None)
    foreign = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        storage_ref=_saved_target(output_id, bucket="foreign"),
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(foreign).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    wrong_namespace = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        storage_ref=_saved_target(output_id, key_prefix="tenant/"),
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(wrong_namespace).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "dependency_changed"

    connection = NativePyramidIoDestination(
        config={
            "endpoint": "http://conn.internal:9000",
            "bucket": "media-cache",
            "base_prefix": None,
        },
        secret_enc="c2VjcmV0",
    )
    target = _saved_target(
        output_id, key_prefix="_aap_managed/media-cache/",
        endpoint="http://conn.internal:9000",
    )
    denied = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        storage_ref=target,
        destination=connection,
        host_allowlist=[],
    )
    with pytest.raises(ImageFailure) as caught:
        await _signer(denied).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "storage_unavailable"
    assert "c2VjcmV0" not in repr(denied)


async def test_host_policy_uses_fallback_only_when_the_database_setting_is_absent(
    monkeypatch,
):
    import yoao_assets_source_runtime.native_storage as native_storage

    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    member = _member(operation_id, etag=None, version_id=None)
    target = _saved_target(
        output_id,
        key_prefix="_aap_managed/media-cache/",
        endpoint="http://conn.internal:9000",
    )
    destination = NativePyramidIoDestination(
        config={
            "endpoint": "http://conn.internal:9000",
            "bucket": "media-cache",
            "base_prefix": None,
        },
        secret_enc="c2VjcmV0",
    )
    seen = []

    def fake_assert(endpoint, allowlist):
        seen.append(list(allowlist))

    monkeypatch.setattr(native_storage, "assert_host_allowed", fake_assert)
    config = _config(
        **{
            **_NATIVE_ENV,
            "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": '["conn.internal"]',
        }
    )
    absent = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        storage_ref=target,
        destination=destination,
        host_allowlist=None,
    )
    with pytest.raises(ImageFailure) as caught:
        await NativeStorageSigner(config, config.native, _Data(absent)).write_reference(
            _write_request(operation_id, lease_token)
        )
    assert caught.value.code == "storage_unavailable"
    assert seen == [["conn.internal"]]

    seen.clear()
    authoritative_empty = _context(
        "put",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=lease_token,
        members=[member],
        storage_ref=target,
        destination=destination,
        host_allowlist=[],
    )
    with pytest.raises(ImageFailure) as caught:
        await NativeStorageSigner(
            config, config.native, _Data(authoritative_empty)
        ).write_reference(_write_request(operation_id, lease_token))
    assert caught.value.code == "storage_unavailable"
    assert seen == [[]]


async def test_frozen_policy_above_sigv4_max_caps_only_issued_lifetimes():
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    member = _member(uuid4())
    context = _context(
        "read",
        output_id=output_id,
        scope_id=operation_id,
        scope_token=token,
        members=[member],
        url_expiry_seconds=2_147_483_647,
        valid_until=None,
    )
    signer = _signer(context)
    inspect = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="inspect",
        )
    )
    assert inspect.expires_in is None
    assert _query(inspect.members[0].get)["X-Amz-Expires"] == "604800"
    assert _query(inspect.members[0].head)["X-Amz-Expires"] == "604800"

    defaulted = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="publish",
        )
    )
    assert defaulted.expires_in == 604800
    assert _query(
        NativePyramidSignedRequest(
            method="GET",
            url=defaulted.members[0].public_url,
            headers={},
            expires_at=defaulted.members[0].public_expires_at,
        )
    )["X-Amz-Expires"] == "604800"

    explicit = await signer.read_references(
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[member.relative_key],
            purpose="publish",
            expires_in=604800,
        )
    )
    assert explicit.expires_in == 604800

    with pytest.raises(ImageFailure) as caught:
        await signer.read_references(
            NativePyramidReadIoRequest(
                operation_id=operation_id,
                token=token,
                relative_keys=[member.relative_key],
                purpose="publish",
                expires_in=604801,
            )
        )
    assert caught.value.code == "invalid_request"


def test_native_configuration_is_all_or_nothing_and_role_separated():
    assert _config().native is None
    with pytest.raises(ConfigurationError):
        _config(
            ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN="w" * 40,
        )
    environment = {**_NATIVE_ENV}
    environment["ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN"] = "w" * 40
    with pytest.raises(ConfigurationError):
        _config(**environment)
    environment = {**_NATIVE_ENV}
    environment["ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY"] = "source-access"
    with pytest.raises(ConfigurationError):
        _config(**environment)


class _ApiRuntime:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.calls = []

    def _result(self, request):
        output_id = uuid4()
        storage_ref = _saved_target(output_id)
        member = _member(request.operation_id, etag=None, version_id=None)
        signed = NativePyramidSignedRequest(
            method="PUT",
            url=(
                "http://minio.internal:9000/media-cache/object"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            ),
            headers={"if-none-match": "*"},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        return NativePyramidIoReferences(
            mode="put",
            scope_id=request.operation_id,
            scope_token=request.lease_token,
            output_id=output_id,
            storage_ref=storage_ref,
            members=[NativePyramidSignedMember(member=member, put=signed)],
            expires_in=None,
        )

    async def native_write_reference(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        return self._result(request)

    async def native_read_references(self, request):
        raise AssertionError("wrong-role token reached the Native reader")


def _api_client(config, runtime):
    return TestClient(create_app(config=config, runtime=runtime))


def test_native_routes_require_their_own_role_token():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    body = _write_request(operation_id, lease_token).model_dump(mode="json")
    runtime = _ApiRuntime()
    with _api_client(_config(**_NATIVE_ENV), runtime) as client:
        denied = client.post(NATIVE_WRITE_REFERENCE_PATH, json=body)
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "permission_denied"

        wrong_role = client.post(
            NATIVE_WRITE_REFERENCE_PATH,
            headers={"Authorization": "Bearer " + "r" * 40},
            json=body,
        )
        assert wrong_role.status_code == 401

        for ordinary in ("s" * 40, "v" * 40):
            ordinary_denied = client.post(
                NATIVE_WRITE_REFERENCE_PATH,
                headers={"Authorization": "Bearer " + ordinary},
                json=body,
            )
            assert ordinary_denied.status_code == 401

        accepted = client.post(
            NATIVE_WRITE_REFERENCE_PATH,
            headers={"Authorization": "Bearer " + "w" * 40},
            json=body,
        )
        assert accepted.status_code == 200
        assert accepted.json()["schema"] == images.IMAGE_SCHEMA
        assert accepted.json()["ok"] is True

        read_denied = client.post(
            NATIVE_READ_REFERENCES_PATH,
            headers={"Authorization": "Bearer " + "w" * 40},
            json={
                "operation_id": str(uuid4()),
                "token": str(uuid4()),
                "relative_keys": ["overview.webp"],
                "purpose": "inspect",
            },
        )
        assert read_denied.status_code == 401
    assert len(runtime.calls) == 1


def test_native_routes_fail_clearly_when_disabled_or_misused():
    with _api_client(_config(), _ApiRuntime()) as client:
        disabled = client.post(
            NATIVE_WRITE_REFERENCE_PATH,
            headers={"Authorization": "Bearer " + "w" * 40},
            json={},
        )
        assert disabled.status_code == 503
        assert disabled.json()["error"]["code"] == "source_unavailable"

    runtime = _ApiRuntime(failure=ImageFailure("lease_lost"))
    with _api_client(_config(**_NATIVE_ENV), runtime) as client:
        invalid = client.post(
            NATIVE_WRITE_REFERENCE_PATH,
            headers={"Authorization": "Bearer " + "w" * 40},
            json={"operation_id": str(uuid4())},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"

        failure = client.post(
            NATIVE_WRITE_REFERENCE_PATH,
            headers={"Authorization": "Bearer " + "w" * 40},
            json=_write_request(uuid4(), uuid4()).model_dump(mode="json"),
        )
        assert failure.status_code == 409
        assert failure.json()["error"]["code"] == "lease_lost"

    cleanup_prefix = NativePyramidRetirementPrefixIoRequest(
        output_id=uuid4(), lease_token=uuid4(), page_id=uuid4(), kind="objects"
    )
    assert cleanup_prefix.kind == "objects"


async def test_multipart_cursor_is_signed_only_for_the_captured_bucket():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    context = _context(
        "retirement_prefix", output_id=output_id, scope_id=page_id,
        scope_token=lease_token, members=[],
    )
    result = await _signer(context).retirement_prefix_reference(
        NativePyramidRetirementPrefixIoRequest(
            output_id=output_id, lease_token=lease_token, page_id=page_id,
            kind="uploads", key_marker="other prefix/key", upload_id_marker="part+/=",
        )
    )
    query = _query(result.request)
    assert urlsplit(result.request.url).path == "/media-cache"
    assert query["key-marker"] == "other prefix/key"
    assert query["upload-id-marker"] == "part+/="
    assert query["max-uploads"] == "100"
    assert "prefix" not in query
    assert result.request.expires_at <= context.valid_until
