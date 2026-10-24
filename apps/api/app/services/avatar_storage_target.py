"""Capture registered avatar targets; perform physical checks only under admission."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import uuid

from botocore.exceptions import ClientError

from app.config import settings
from app.db.models.storage_connection import StorageConnection
from app.services import connector_guard, storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.sources.s3_reference import S3Access

MANAGED_NAMESPACE = "_aap_managed/avatars/"


def managed_prefix(base):
    """Keep one canonical, bounded S3 prefix without changing its Unicode bytes."""
    from app.services.avatar_storage import AvatarStorageError

    if not isinstance(base, str):
        raise AvatarStorageError("avatar_storage_binding_unproven")
    base = base.removesuffix("/")
    if (
        "\\" in base
        or any(ord(char) < 32 or ord(char) == 127 for char in base)
        or (base and any(part in {"", ".", ".."} for part in base.split("/")))
    ):
        raise AvatarStorageError("avatar_storage_binding_unproven")
    result = (base + "/" if base else "") + MANAGED_NAMESPACE
    if len(result.encode("utf-8")) > 980:
        raise AvatarStorageError("avatar_storage_binding_unproven")
    return result


@dataclass(frozen=True)
class Target:
    location_id: uuid.UUID
    config_revision: int
    connection_revision: int | None
    endpoint_url: str
    bucket: str
    key_prefix: str
    _access: S3Access | None = field(default=None, repr=False, compare=False)
    _legacy_client: object = field(default=None, repr=False, compare=False)

    @property
    def facts(self):
        return {
            "location_id": str(self.location_id),
            "config_revision": self.config_revision,
            "connection_revision": self.connection_revision,
            "endpoint_url": self.endpoint_url,
            "bucket": self.bucket,
            "key_prefix": self.key_prefix,
        }

    @contextmanager
    def client(self):
        if self._access is None:
            # The process owns the shared legacy client; do not close it here.
            yield self._legacy_client
        else:
            with self._access.client() as client:
                yield client


async def target_for(db, location, *, require_enabled=True):
    """Capture metadata and credentials without DNS, network I/O or binding locks."""
    from app.services import avatar_storage as owners

    if (
        location is None
        or location.kind != "s3"
        or (require_enabled and not location.enabled)
        or type(location.config_revision) is not int
        or location.config_revision < 1
    ):
        raise owners.AvatarStorageError("avatar_storage_binding_unproven")
    if location.id == LEGACY_LOCATION_ID:
        if (
            location.config_revision != 1
            or location.config
            != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
            or getattr(location, "connection_id", None) is not None
        ):
            raise owners.AvatarStorageError("avatar_storage_binding_unproven")
        return Target(
            location.id,
            location.config_revision,
            None,
            owners._runtime_endpoint(),
            owners.storage_service.avatars_bucket,
            "",
            _legacy_client=owners.storage_service.client,
        )
    connection_id = getattr(location, "connection_id", None)
    connection = (
        await db.get(StorageConnection, connection_id, populate_existing=True)
        if connection_id is not None
        else None
    )
    if (
        connection is None
        or connection.kind != "s3"
        or connection.scope != "global"
        or connection.project_id is not None
        or not connection.secret_enc
        or type(connection.revision) is not int
        or connection.revision < 1
    ):
        raise owners.AvatarStorageError("avatar_storage_binding_unproven")
    access = S3Access(
        dict(connection.config),
        connection.secret_enc,
        tuple(await connector_guard.get_allowlist(db)),
    )
    # S3Access.client repeats the live host guard inside the registered I/O.
    # Resolving DNS here would put physical work inside allocation row locks.
    try:
        endpoint = owners._endpoint(access.endpoint)
        bucket = connection.config["bucket"]
        if not owners._plain(bucket, 255) or any(char in bucket for char in "/\\?#@"):
            raise owners.AvatarStorageError("avatar_storage_binding_unproven")
        prefix = managed_prefix(connection.config.get("base_prefix", ""))
    except (KeyError, TypeError, ValueError):
        raise owners.AvatarStorageError("avatar_storage_binding_unproven") from None
    return Target(
        location.id,
        location.config_revision,
        connection.revision,
        endpoint,
        bucket,
        prefix,
        _access=access,
    )


def version_id(response):
    from app.services.avatar_storage import AvatarStorageError

    result = response.get("VersionId")
    if result is not None and (
        not isinstance(result, str)
        or not 0 < len(result) <= 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
    ):
        raise AvatarStorageError("avatar_storage_invalid_receipt")
    return result


def object_params(bucket, key, version=None):
    return {
        "Bucket": bucket,
        "Key": key,
        **({"VersionId": version} if version is not None else {}),
    }


def _read_probe(client, params, expected):
    from app.services.avatar_storage import AvatarStorageError

    response = client.get_object(**params)
    with storage_access.closing_registered(response["Body"]) as body:
        actual = body.read(len(expected) + 1)
        if actual != expected or body.read(1):
            raise AvatarStorageError("avatar_storage_invalid_receipt")
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
    ):
        raise AvatarStorageError("avatar_storage_invalid_receipt")
    if (
        params.get("VersionId") is not None
        and response.get("VersionId") != params["VersionId"]
    ):
        raise AvatarStorageError("avatar_storage_invalid_receipt")


def _prove_absent(client, params):
    from app.services.avatar_storage import AvatarStorageError

    try:
        response = client.get_object(**params)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "NoSuchKey",
            "NoSuchVersion",
            "404",
            "NotFound",
        }:
            return
        raise
    storage_access.close_registered(response["Body"])
    raise AvatarStorageError("avatar_storage_probe_cleanup_unproven")


def _conditional_put(client, params, data):
    try:
        return client.put_object(**params, Body=data, IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "AccessDenied",
            "SignatureDoesNotMatch",
            "InvalidAccessKeyId",
            "403",
            "NoSuchBucket",
            "404",
            "InvalidRequest",
            "400",
            "PreconditionFailed",
            "412",
            "ConditionalRequestConflict",
            "409",
        }:
            raise
        raise storage_access.mark_unsettled(exc)
    except BaseException as exc:
        # A lost PUT response cannot be converted to proven deletion.
        raise storage_access.mark_unsettled(exc)


def probe_target(target):
    """Prove controlled creation, full readback and exact cleanup at this target."""
    from app.services.avatar_storage import AvatarStorageError

    nonce = uuid.uuid4().hex
    key = f"{target.key_prefix}.probe-{nonce}"
    data = hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).digest()
    with target.client() as client:
        params = object_params(target.bucket, key)
        response = _conditional_put(client, params, data)
        try:
            version = version_id(response)
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        params = object_params(target.bucket, key, version)
        try:
            _read_probe(client, params, data)
            try:
                unexpected = _conditional_put(
                    client, object_params(target.bucket, key), b"conflict"
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                    "409",
                }:
                    raise
            else:
                # An ignored condition may have created another provider version;
                # never call that a successfully qualified managed target.
                try:
                    unexpected_params = object_params(
                        target.bucket, key, version_id(unexpected)
                    )
                    client.delete_object(**unexpected_params)
                    _prove_absent(client, unexpected_params)
                except BaseException as exc:
                    raise storage_access.mark_unsettled(exc)
                raise AvatarStorageError("avatar_storage_conditions_unsupported")
        finally:
            try:
                client.delete_object(**params)
                _prove_absent(client, params)
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
    return {"conditional_write": True, "full_read": True, "delete": True}
