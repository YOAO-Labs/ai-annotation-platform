"""Registered S3 targets and physical probes for the reviewed purposes."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.db.models.storage_connection import StorageConnection
from app.services import connector_guard, storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.sources.s3_reference import S3Access

PURPOSES = {
    "avatars",
    "bug-reports",
    "audit-archive",
    "export",
    "import",
    "datasets",
    "media-cache",
    "annotations",
    "training",
}


def owner_module(purpose):
    if purpose == "training":
        from app.services import training_dataset_storage

        return training_dataset_storage
    if purpose == "avatars":
        from app.services import avatar_storage

        return avatar_storage
    if purpose == "bug-reports":
        from app.services import bug_attachment_storage

        return bug_attachment_storage
    if purpose == "audit-archive":
        from app.services import audit_archive_storage

        return audit_archive_storage
    if purpose == "export":
        from app.services import export_storage

        return export_storage
    if purpose == "import":
        from app.services import import_storage

        return import_storage
    if purpose == "media-cache":
        from app.services import media_cache_storage

        return media_cache_storage
    if purpose == "annotations":
        from app.services import annotations_storage

        return annotations_storage
    if purpose == "datasets":
        from app.services import dataset_storage

        return dataset_storage
    raise ValueError("Unsupported managed S3 purpose")


def error(purpose, suffix):
    owners = owner_module(purpose)
    if purpose == "training":
        return owners.TrainingDatasetError(
            "training_storage_" + suffix,
            "训练文件存储归属或目标能力无法核实",
            status_code=409,
        )
    cls, prefix = (
        (owners.AvatarStorageError, "avatar_storage_")
        if purpose == "avatars"
        else (owners.BugAttachmentStorageError, "bug_attachment_storage_")
        if purpose == "bug-reports"
        else (owners.AuditArchiveStorageError, "audit_archive_storage_")
        if purpose == "audit-archive"
        else (owners.ImportStorageError, "import_storage_")
        if purpose == "import"
        else (owners.MediaCacheStorageError, "media_cache_storage_")
        if purpose == "media-cache"
        else (owners.AnnotationsStorageError, "annotations_storage_")
        if purpose == "annotations"
        else (owners.DatasetStorageError, "dataset_storage_")
        if purpose == "datasets"
        else (owners.ExportStorageError, "export_storage_")
    )
    return cls(prefix + suffix)


def managed_prefix(base, *, purpose="avatars"):
    """Keep one canonical, bounded S3 prefix without changing its Unicode bytes."""

    if not isinstance(base, str):
        raise error(purpose, "binding_unproven")
    base = base.removesuffix("/")
    if (
        "\\" in base
        or any(ord(char) < 32 or ord(char) == 127 for char in base)
        or (base and any(part in {"", ".", ".."} for part in base.split("/")))
    ):
        raise error(purpose, "binding_unproven")
    result = (base + "/" if base else "") + f"_aap_managed/{purpose}/"
    if purpose == "training":
        from app.services.reference_keys import is_reference

        try:
            valid = (
                len(result) <= 266
                and len(result.encode("utf-8")) <= 707
                and not is_reference(result)
            )
        except UnicodeError:
            valid = False
        if not valid:
            raise error(purpose, "binding_unproven")
        return result
    if purpose == "media-cache":
        if len(result.encode("utf-8")) > 436:
            raise error(purpose, "binding_unproven")
        return result
    if purpose == "annotations":
        if len(result) > 896 or len(result.encode("utf-8")) > 920:
            raise error(purpose, "binding_unproven")
        return result
    if purpose == "datasets":
        if len(result) > 891 or len(result.encode("utf-8")) > 915:
            raise error(purpose, "binding_unproven")
        return result
    if len(result.encode("utf-8")) > (
        980
        if purpose == "avatars"
        else 904
        if purpose == "export"
        else 645
        if purpose == "import"
        else 920
    ):
        raise error(purpose, "binding_unproven")
    return result


@dataclass(frozen=True)
class Target:
    location_id: uuid.UUID
    config_revision: int
    connection_revision: int | None
    endpoint_url: str
    bucket: str
    key_prefix: str
    purpose: str = field(default="avatars", repr=False, compare=False)
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
    def client(self, *, read_timeout=None, single_attempt=False, owned=False):
        if self._access is None and owned:
            from app.services.storage import StorageService

            # Managed ZIP transfer keeps the original SDK retry/transport policy.
            # Its observer belongs to this exclusive client, never the shared one.
            try:
                client = StorageService(
                    client_config=self._legacy_client.meta.config
                ).client
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            try:
                yield client
            finally:
                storage_access.close_registered(client)
        elif self._access is None and single_attempt:
            from app.services.storage import StorageService

            # Accounting owns this transport: one durable intent represents one
            # submission, including when the provider loses its response.
            config = Config(
                connect_timeout=3,
                read_timeout=read_timeout or 30,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
            try:
                client = StorageService(client_config=config).client
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            try:
                yield client
            finally:
                storage_access.close_registered(client)
        elif self._access is None:
            # The process owns the shared legacy client; do not close it here.
            yield self._legacy_client
        else:
            options = {} if read_timeout is None else {"read_timeout": read_timeout}
            with self._access.client(**options) as client:
                yield client


async def target_for(db, location, *, purpose, require_enabled=True):
    """Capture metadata and credentials without DNS, network I/O or binding locks."""
    owners = owner_module(purpose)

    if (
        location is None
        or location.kind != "s3"
        or (require_enabled and location.enabled is not True)
        or type(location.config_revision) is not int
        or location.config_revision < 1
    ):
        raise error(purpose, "binding_unproven")
    if location.id == LEGACY_LOCATION_ID:
        if (
            location.config_revision != 1
            or (
                purpose
                in {
                    "bug-reports",
                    "audit-archive",
                    "export",
                    "import",
                    "datasets",
                    "media-cache",
                    "annotations",
                    "training",
                }
                and type(location.config.get("use_ssl")) is not bool
            )
            or location.config
            != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
            or getattr(location, "connection_id", None) is not None
        ):
            raise error(purpose, "binding_unproven")
        return Target(
            location.id,
            location.config_revision,
            None,
            owners._runtime_endpoint(),
            getattr(
                owners.storage_service,
                {
                    "avatars": "avatars_bucket",
                    "bug-reports": "bug_reports_bucket",
                    "audit-archive": "audit_archive_bucket",
                    "export": "export_bucket",
                    "import": "import_bucket",
                    "datasets": "datasets_bucket",
                    "media-cache": "media_cache_bucket",
                    "annotations": "bucket",
                    "training": "training_bucket",
                }[purpose],
            ),
            "",
            purpose=purpose,
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
        raise error(purpose, "binding_unproven")
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
            raise error(purpose, "binding_unproven")
        prefix = managed_prefix(
            connection.config.get("base_prefix", ""), purpose=purpose
        )
    except (KeyError, TypeError, ValueError):
        raise error(purpose, "binding_unproven") from None
    return Target(
        location.id,
        location.config_revision,
        connection.revision,
        endpoint,
        bucket,
        prefix,
        purpose=purpose,
        _access=access,
    )


def version_id(response, *, purpose="avatars"):

    result = response.get("VersionId")
    if purpose == "import" and result in (None, "", "null"):
        raise error(purpose, "versioning_required")
    if result is not None and (
        not isinstance(result, str)
        or not 0 < len(result) <= 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
    ):
        raise error(purpose, "invalid_receipt")
    return result


def version_matches(expected, actual):
    """An explicit S3 null version can omit its response header."""
    return expected == actual or (expected == "null" and actual is None)


def object_params(bucket, key, version=None):
    return {
        "Bucket": bucket,
        "Key": key,
        **({"VersionId": version} if version is not None else {}),
    }


def _read_probe(client, params, expected, *, purpose="avatars"):

    response = client.get_object(**params)
    with storage_access.closing_registered(response["Body"]) as body:
        actual = body.read(len(expected) + 1)
        if actual != expected or body.read(1):
            raise error(purpose, "invalid_receipt")
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
    ):
        raise error(purpose, "invalid_receipt")
    if params.get("VersionId") is not None and not version_matches(
        params["VersionId"], response.get("VersionId")
    ):
        raise error(purpose, "invalid_receipt")


def _prove_absent(client, params, *, purpose="avatars"):

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
    raise error(purpose, "probe_cleanup_unproven")


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

    purpose = target.purpose
    nonce = uuid.uuid4().hex
    key = f"{target.key_prefix}.probe-{nonce}"
    data = hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).digest()
    with target.client() as client:
        params = object_params(target.bucket, key)
        response = _conditional_put(client, params, data)
        try:
            version = version_id(response, purpose=purpose)
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        params = object_params(target.bucket, key, version)
        try:
            _read_probe(client, params, data, purpose=purpose)
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
                        target.bucket, key, version_id(unexpected, purpose=purpose)
                    )
                    client.delete_object(**unexpected_params)
                    _prove_absent(client, unexpected_params, purpose=purpose)
                except BaseException as exc:
                    raise storage_access.mark_unsettled(exc)
                raise error(purpose, "conditions_unsupported")
        finally:
            try:
                client.delete_object(**params)
                _prove_absent(client, params, purpose=purpose)
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
    return {"conditional_write": True, "full_read": True, "delete": True}
