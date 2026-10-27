"""Closed import locators, target capabilities and upload publication proofs."""

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
import pytest

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import connector_guard, storage_access, storage_presigning
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.managed_s3_target import Target
from tests.test_avatar_storage_target import Objects

LOCATION = uuid.UUID(int=821)
ENDPOINT = "https://saved-import.example.test"
ROOT = "team/图片/_aap_managed/import/"


@pytest.fixture
def storage():
    from app.services import import_storage

    return import_storage


@pytest.fixture
def targets():
    from app.services import import_storage_target

    return import_storage_target


def logical_prefix(family="prediction-inputs"):
    if family == "mask-formats":
        return f"mask-formats/{uuid.UUID(int=1)}/{uuid.UUID(int=2)}/{uuid.UUID(int=3)}/"
    return f"{family}/{uuid.UUID(int=3)}/"


def reference(prefix=None, **changes):
    return {
        "schema": 2,
        "purpose": "import",
        "location_id": str(LOCATION),
        "config_revision": 3,
        "binding_generation": 2,
        "endpoint_url": ENDPOINT,
        "bucket": "import-saved",
        "key_prefix": ROOT + (prefix or logical_prefix()),
        "storage_identity": managed_storage_identity(ENDPOINT, "import-saved"),
        **changes,
    }


@pytest.mark.parametrize(
    "family", ["prediction-inputs", "__aap_upload_staging__", "mask-formats"]
)
def test_three_logical_families_keep_exact_saved_physical_prefix(storage, family):
    logical = logical_prefix(family)
    ref = reference(logical)
    owner = storage.resolve_ref(ref, expected_prefix=logical)
    ref["key_prefix"] = "changed"
    assert owner.ref["key_prefix"] == ROOT + logical
    assert owner.key("payload") == ROOT + logical + "payload"
    assert owner.location_id == LOCATION


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 3),
        ("purpose", "export"),
        ("config_revision", True),
        ("config_revision", 1.5),
        ("binding_generation", True),
        ("binding_generation", 1),
        ("location_id", "invalid"),
        ("location_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("endpoint_url", "https://private@host.test"),
        ("endpoint_url", ENDPOINT + "/changed"),
        ("bucket", "other/bucket"),
        ("bucket", "encoded%bucket"),
        ("storage_identity", "0" * 64),
        ("key_prefix", "../_aap_managed/import/" + logical_prefix()),
        ("key_prefix", "team//_aap_managed/import/" + logical_prefix()),
        ("key_prefix", "team/_aap_managed/export/" + logical_prefix()),
        ("key_prefix", ROOT + logical_prefix("__aap_upload_staging__")),
    ],
)
def test_saved_ref_rejects_changed_identity_or_uncontrolled_prefix(
    storage, field, value
):
    with pytest.raises(storage.ImportStorageError):
        storage.resolve_ref(
            reference(**{field: value}), expected_prefix=logical_prefix()
        )


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_ref_has_exactly_nine_fields(storage, mutation):
    ref = reference()
    if mutation == "missing":
        ref.pop("storage_identity")
    else:
        ref["secret"] = "must-not-be-saved"
    with pytest.raises(storage.ImportStorageError):
        storage.resolve_ref(ref, expected_prefix=logical_prefix())


@pytest.mark.parametrize("unicode", [False, True])
def test_prefix_reserves_longest_mask_key_in_utf8(storage, targets, unicode):
    namespace = targets.MANAGED_NAMESPACE
    budget = targets.MAX_MANAGED_PREFIX_BYTES - len(namespace.encode()) - 1
    base = ("图" * (budget // 3) + "a" * (budget % 3)) if unicode else "a" * budget
    root = targets.managed_prefix(base)
    assert len(root.encode()) == 645
    logical = logical_prefix("mask-formats")
    owner = storage.resolve_ref(
        reference(logical, key_prefix=root + logical), expected_prefix=logical
    )
    assert len(owner.key("a" * 255).encode()) == 1024
    with pytest.raises(storage.ImportStorageError):
        owner.key("a" * 256)
    with pytest.raises(storage.ImportStorageError):
        targets.managed_prefix(base + "a")


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "../payload",
        "./payload",
        "a//payload",
        "a\\payload",
        "/payload",
        "a\npayload",
    ],
)
def test_relative_key_cannot_escape_saved_owner(storage, relative):
    owner = storage.resolve_ref(reference(), expected_prefix=logical_prefix())
    with pytest.raises(storage.ImportStorageError):
        owner.key(relative)


class ProbeObjects(Objects):
    def __init__(self, *, status="Enabled", version="version"):
        super().__init__(version)
        self.status = status
        self.list_response = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        self.version_response = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        self.deny_list = None

    def get_bucket_versioning(self, **params):
        self.calls.append(("versioning", params))
        return {"Status": self.status} if self.status is not None else {}

    def list_objects_v2(self, **params):
        self.calls.append(("list", params))
        if self.deny_list == "list":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        return self.list_response

    def list_object_versions(self, **params):
        self.calls.append(("versions", params))
        if self.deny_list == "versions":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectVersions")
        return self.version_response


def probe_target(objects):
    return Target(
        LOCATION,
        3,
        4,
        ENDPOINT,
        "import-saved",
        ROOT,
        purpose="import",
        _access=objects,
    )


def test_import_probe_proves_versions_and_both_scoped_lists(targets):
    objects = ProbeObjects()
    result = targets.probe_target(probe_target(objects))
    assert result == {
        "conditional_write": True,
        "full_read": True,
        "delete": True,
        "versioned": True,
        "list": True,
        "list_versions": True,
    }
    assert not objects.objects and all(body.closed for body in objects.bodies)
    lists = [
        (kind, params) for kind, params in objects.calls if kind in {"list", "versions"}
    ]
    assert {kind for kind, _ in lists} == {"list", "versions"}
    assert lists[0][1] == lists[1][1]
    assert lists[0][1]["Prefix"].startswith(ROOT + ".probe-list-")
    assert lists[0][1]["MaxKeys"] == 1
    assert objects.closed == 3


@pytest.mark.parametrize("status", [None, "Suspended"])
def test_probe_rejects_nonversioned_bucket_before_put(storage, targets, status):
    objects = ProbeObjects(status=status)
    with pytest.raises(storage.ImportStorageError) as caught:
        targets.probe_target(probe_target(objects))
    assert caught.value.code == "import_storage_versioning_required"
    assert [kind for kind, _ in objects.calls] == ["versioning"]
    assert not objects.objects and objects.closed == 1


@pytest.mark.parametrize("version", [None, "null"])
def test_enabled_bucket_must_return_an_actual_put_version(storage, targets, version):
    objects = ProbeObjects(version=version)
    with pytest.raises(storage.ImportStorageError) as caught:
        targets.probe_target(probe_target(objects))
    assert caught.value.code == "import_storage_versioning_required"
    assert caught.value.storage_operation_unsettled
    assert not any(kind in {"list", "versions"} for kind, _ in objects.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "list_denied",
        "versions_denied",
        "list_status",
        "versions_status",
        "contents",
        "delete_markers",
    ],
)
def test_probe_requires_usable_empty_scoped_lists(storage, targets, failure):
    objects = ProbeObjects()
    if failure.endswith("_denied"):
        objects.deny_list = failure.removesuffix("_denied")
    elif failure == "list_status":
        objects.list_response = {"ResponseMetadata": {"HTTPStatusCode": 503}}
    elif failure == "versions_status":
        objects.version_response = {"ResponseMetadata": {"HTTPStatusCode": 503}}
    elif failure == "contents":
        objects.list_response = {
            **objects.list_response,
            "Contents": [{"Key": "unexpected"}],
        }
    else:
        objects.version_response = {
            **objects.version_response,
            "DeleteMarkers": [{"Key": "unexpected"}],
        }
    with pytest.raises((storage.ImportStorageError, ClientError)):
        targets.probe_target(probe_target(objects))
    assert not objects.objects and all(body.closed for body in objects.bodies)
    assert objects.closed == 3


async def test_missing_legacy_registration_uses_only_saved_legacy_bucket(
    storage, monkeypatch
):
    ref = storage.legacy_ref(logical_prefix(), bucket="saved-old-import")
    owner = storage.resolve_ref(ref, expected_prefix=logical_prefix())
    monkeypatch.setattr(storage.storage_service, "import_bucket", "new-runtime-default")
    db = SimpleNamespace(get=AsyncMock(return_value=None), scalar=AsyncMock())
    target = await storage.io_target(db, owner)
    assert (
        target.location_id == LEGACY_LOCATION_ID and target.bucket == "saved-old-import"
    )
    assert target.endpoint_url == ref["endpoint_url"] and target.key_prefix == ""
    assert db.get.await_args.args == (StorageLocation, LEGACY_LOCATION_ID)
    db.scalar.assert_not_awaited()


@pytest.mark.parametrize("legacy", [False, True])
async def test_schema2_never_uses_missing_location_fallback(storage, legacy):
    ref = reference()
    if legacy:
        ref = {
            **storage.legacy_ref(logical_prefix()),
            "schema": 2,
            "binding_generation": 2,
        }
    owner = storage.resolve_ref(ref, expected_prefix=logical_prefix())
    with pytest.raises(storage.ImportStorageError):
        await storage.io_target(
            SimpleNamespace(get=AsyncMock(return_value=None)), owner
        )


async def test_saved_external_owner_captures_current_credentials_without_binding_or_io(
    storage, monkeypatch
):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    connection = StorageConnection(
        id=uuid.uuid4(),
        kind="s3",
        scope="global",
        project_id=None,
        revision=7,
        config={
            "endpoint": ENDPOINT,
            "bucket": "import-saved",
            "base_prefix": "team/图片",
            "use_ssl": True,
        },
        secret_enc=encrypt_secret(
            {"access_key": "rotated-access", "secret_key": "rotated-secret"}
        ),
    )
    location = StorageLocation(
        id=LOCATION,
        kind="s3",
        enabled=False,
        config_revision=5,
        config={},
        connection_id=connection.id,
    )

    async def get(model, _identifier, **_kwargs):
        if model is StorageLocation:
            return location
        if model is StorageConnection:
            return connection
        assert model is not StorageBinding, (
            "saved reads must not consult the current binding"
        )
        pytest.fail("unexpected metadata lookup")

    monkeypatch.setattr(
        connector_guard,
        "get_allowlist",
        AsyncMock(return_value=("saved-import.example.test",)),
    )
    db = SimpleNamespace(get=get, scalar=AsyncMock(return_value=location))
    owner = storage.resolve_ref(reference(), expected_prefix=logical_prefix())
    target = await storage.io_target(db, owner)
    assert target.config_revision == 5 and target.connection_revision == 7
    assert target._access.secret_enc == connection.secret_enc
    assert (
        target.endpoint_url == owner.ref["endpoint_url"]
        and target.bucket == owner.bucket
    )
    assert owner.ref["config_revision"] == 3


@pytest.fixture
def signing(storage, monkeypatch):
    events = []

    class Access:
        @contextmanager
        def client(self):
            events.append("client-open")
            try:
                yield SimpleNamespace(marker="saved-credentials")
            finally:
                events.append("client-close")

    owner = storage.resolve_ref(reference(), expected_prefix=logical_prefix())
    state = {
        "target": Target(
            LOCATION,
            3,
            4,
            ENDPOINT,
            owner.bucket,
            ROOT,
            purpose="import",
            _access=Access(),
        ),
        "revision": 2,
        "after_sign": None,
    }

    async def current(_db, _owner, *, lock=False):
        events.append(("metadata", lock))
        assert _owner.ref == owner.ref
        return state["target"]

    async def check(_db, _location, *, expected_revision=None, lock=False):
        if expected_revision is not None and expected_revision != state["revision"]:
            raise ContentError("storage_maintenance_changed")
        return state["revision"]

    @asynccontextmanager
    async def permit():
        events.append("permit-open")
        try:
            yield SimpleNamespace(revision=state["revision"])
        finally:
            events.append("permit-close")

    async def acquire(*_args, **_kwargs):
        return permit()

    def sign(client, operation, *, params, expires_in, audience):
        events.append(("sign", operation, params, expires_in, audience))
        assert client.marker == "saved-credentials"
        if state["after_sign"] is not None:
            state["after_sign"]()
        return ENDPOINT + "/signed"

    monkeypatch.setattr(storage, "io_target", current)
    monkeypatch.setattr(storage_access, "check", check)
    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(storage_presigning, "generate_url", sign)
    return owner, state, events


async def test_prepared_url_uses_saved_endpoint_and_closes_before_permit(
    storage, signing
):
    owner, _, events = signing
    before = datetime.now(timezone.utc)
    prepared = await storage.prepare_upload_url(
        None, owner, "payload", "application/json", expires_in=900
    )
    assert isinstance(prepared, str) and isinstance(prepared, storage.PreparedUploadURL)
    assert str(prepared) == ENDPOINT + "/signed"
    sign = next(
        event for event in events if isinstance(event, tuple) and event[0] == "sign"
    )
    assert sign[1] == "put_object" and sign[4] == "internal"
    assert sign[2] == {
        "Bucket": owner.bucket,
        "Key": owner.key("payload"),
        "ContentType": "application/json",
    }
    assert 0 < sign[3] <= 900
    assert (
        before + timedelta(seconds=899)
        <= prepared.deadline
        <= datetime.now(timezone.utc) + timedelta(seconds=900)
    )
    assert events.index("client-close") < events.index("permit-close")


@pytest.mark.parametrize("field", ["config_revision", "connection_revision", "gate"])
async def test_signing_race_rejects_old_proof_but_next_attempt_uses_current_capture(
    storage, signing, field
):
    owner, state, events = signing
    original_ref = deepcopy(owner.ref)

    def changed():
        if field == "gate":
            state["revision"] += 1
        else:
            state["target"] = replace(
                state["target"], **{field: getattr(state["target"], field) + 1}
            )

    state["after_sign"] = changed
    with pytest.raises((storage.ImportStorageError, ContentError)) as caught:
        await storage.prepare_upload_url(None, owner, "payload", "application/json")
    assert caught.value.code == (
        "storage_maintenance_changed"
        if field == "gate"
        else "import_storage_configuration_changed"
    )
    assert events.index("client-close") < events.index("permit-close")
    state["after_sign"] = None
    prepared = await storage.prepare_upload_url(
        None, owner, "payload", "application/json"
    )
    await storage.validate_upload_url(
        None,
        prepared,
        owner=owner,
        relative="payload",
        content_type="application/json",
        lock=True,
    )
    assert owner.ref == original_ref
    assert ("metadata", True) in events


@pytest.mark.parametrize(
    "expected", ["owner", "relative", "content_type", "plain_string", "deadline"]
)
async def test_upload_publication_proof_is_bound_to_exact_owner_key_type_and_deadline(
    storage, signing, expected
):
    owner, state, _ = signing
    prepared = await storage.prepare_upload_url(
        None, owner, "payload", "application/json"
    )
    kwargs = {
        "owner": owner,
        "relative": "payload",
        "content_type": "application/json",
        "lock": True,
    }
    if expected == "owner":
        kwargs["owner"] = storage.resolve_ref(
            reference(logical_prefix("__aap_upload_staging__")),
            expected_prefix=logical_prefix("__aap_upload_staging__"),
        )
    elif expected == "relative":
        kwargs["relative"] = "different"
    elif expected == "content_type":
        kwargs["content_type"] = "image/png"
    elif expected == "plain_string":
        prepared = str(prepared)
    else:
        prepared = storage.PreparedUploadURL(
            str(prepared),
            owner,
            "payload",
            "application/json",
            state["target"].facts,
            state["revision"],
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    with pytest.raises(storage.ImportStorageError) as caught:
        await storage.validate_upload_url(None, prepared, **kwargs)
    assert caught.value.code == (
        "import_storage_expired"
        if expected == "deadline"
        else "import_storage_invalid_receipt"
    )


async def test_late_final_publication_detects_rotation_after_signing(storage, signing):
    owner, state, _ = signing
    prepared = await storage.prepare_upload_url(
        None, owner, "payload", "application/json"
    )
    state["target"] = replace(state["target"], connection_revision=5)
    with pytest.raises(storage.ImportStorageError) as caught:
        await storage.validate_upload_url(
            None,
            prepared,
            owner=owner,
            relative="payload",
            content_type="application/json",
            lock=True,
        )
    assert caught.value.code == "import_storage_configuration_changed"
