"""Metadata-only migration target and finite registered-copy transport."""

import hashlib
import re
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from yoao_assets_source.saved_io import SavedIOError

from app.services import storage_access, storage_registry_data
from app.services.saved_io import saved_io_client
from app.services.source_readers.s3 import directory_prefix
from app.services.storage_identity import LEGACY_LOCATION_ID
from app.services.storage_migration_copy import CopyError, CopyStopped
from app.services.storage_migration_inventories import reject


async def lock_scope(db, target_location_id):
    """Preserve connection-before-location SHARE order in the caller transaction."""
    locations = sorted({LEGACY_LOCATION_ID, target_location_id})
    connections = set()
    for location_id in locations:
        location = await storage_registry_data.get_location(db, location_id)
        if location is not None and location.connection_id is not None:
            connections.add(location.connection_id)
    for connection_id in sorted(connections):
        await storage_registry_data.get_connection(db, connection_id, lock="share")
    for location_id in locations:
        await storage_registry_data.get_location(db, location_id, lock="share")


async def facts(db, location_id, *, lock=False):
    location = await storage_registry_data.get_location(db, location_id)
    if location is None or location.connection_id is None:
        reject("copy_target_unavailable")
    mode = "share" if lock else None
    connection = await storage_registry_data.get_connection(
        db, location.connection_id, lock=mode
    )
    current = await storage_registry_data.get_location(db, location_id, lock=mode)
    if (
        connection is None
        or current is None
        or current.connection_id != connection.id
        or not current.enabled
        or current.kind != "s3"
        or connection.kind != "s3"
        or connection.scope != "global"
        or connection.project_id is not None
        or not connection.secret_set
    ):
        reject("copy_target_unavailable")
    try:
        endpoint = connection.config["endpoint"]
        if "://" not in endpoint:
            endpoint = (
                "https://" if connection.config.get("use_ssl") else "http://"
            ) + endpoint
        parts = urlsplit(endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError
        prefix = directory_prefix(connection.config.get("base_prefix", ""))
        bucket = connection.config["bucket"]
    except (KeyError, ValueError, TypeError, AttributeError):
        reject("copy_target_unavailable")
    root = prefix + "_aap_migration/"
    if len(root.encode("utf-8")) > 800:
        reject("copy_target_prefix_budget")
    identity = {
        "location_id": str(current.id),
        "connection_id": str(connection.id),
        "endpoint_url": endpoint.rstrip("/"),
        "bucket": bucket,
        "base_prefix": prefix,
        "key_prefix": root,
    }
    revisions = {"location": current.config_revision, "connection": connection.revision}
    return identity, revisions, connection.name


def response_metadata(metadata):
    result = dict(metadata)
    for name in ("LastModified", "Expires"):
        if isinstance(result.get(name), str):
            result[name] = datetime.fromisoformat(result[name])
    return result


def provider_error(exc, operation):
    """Preserve S3 error classification without exposing request diagnostics."""
    if exc.storage_code:
        error = ClientError(
            {
                "Error": {"Code": exc.storage_code},
                "ResponseMetadata": {"HTTPStatusCode": exc.status_code},
            },
            operation,
        )
    else:
        error = CopyError(exc.code)
    return storage_access.mark_unsettled(error) if exc.unsettled else error


class DownloadBody:
    def __init__(self, context, response):
        self.context, self.response, self.closed = context, response, False

    def read(self, size=-1):
        return self.response.read(size)

    def close(self):
        if not self.closed:
            self.closed = True
            self.context.__exit__(None, None, None)


class _CopyBody(DownloadBody):
    def read(self, size=-1):
        try:
            return super().read(size)
        except SavedIOError as exc:
            _CopyIO._error(exc, "copy_get")


class _CopyIO:
    """Only the migration kernel's five finite operations; no SDK handle."""

    def __init__(self, channel, proof, identity):
        self.channel, self.proof, self.identity = channel, proof, identity
        self.body = None

    def close(self):
        if self.body is not None:
            storage_access.close_registered(self.body)

    def _object(self, bucket, key, **extra):
        root = self.identity["key_prefix"]
        if (
            bucket != self.identity["bucket"]
            or not isinstance(key, str)
            or not key.startswith(root)
        ):
            raise CopyError("target_identity_mismatch")
        parts = key[len(root) :].split("/")
        if (
            len(parts) != 3
            or re.fullmatch(r"[a-f0-9-]{36}", parts[0]) is None
            or re.fullmatch(r"[a-f0-9]{64}", parts[1]) is None
            or re.fullmatch(r"[a-f0-9]{64}", parts[2]) is None
        ):
            raise CopyError("target_identity_mismatch")
        return {
            **self.proof,
            "object_id": parts[1],
            "obligation": hashlib.sha256(key.encode()).hexdigest(),
            **extra,
        }

    @staticmethod
    def _error(exc, action):
        if exc.unsettled:
            raise storage_access.mark_unsettled(
                CopyStopped("storage_operation_unsettled")
            ) from None
        raise provider_error(exc, action) from None

    def _command(self, action, subject):
        try:
            return self.channel.command("migration", action, subject)
        except SavedIOError as exc:
            self._error(exc, action)

    def get_bucket_versioning(self, *, Bucket):
        if Bucket != self.identity["bucket"]:
            raise CopyError("target_identity_mismatch")
        return self._command("copy_versioning", self.proof)

    def list_object_versions(self, *, Bucket, Prefix, MaxKeys):
        if MaxKeys != 2:
            raise CopyError("target_history_conflict")
        return self._command("copy_history", self._object(Bucket, Prefix))

    def head_object(self, *, Bucket, Key, VersionId=None):
        return response_metadata(
            self._command("copy_head", self._object(Bucket, Key, version=VersionId))
        )

    def get_object(self, *, Bucket, Key, VersionId):
        context = self.channel.download(
            "migration", "copy_get", self._object(Bucket, Key, version=VersionId)
        )
        try:
            result = context.__enter__()
        except SavedIOError as exc:
            self._error(exc, "copy_get")
        body = self.body = _CopyBody(context, result)
        try:
            return {**response_metadata(result.metadata), "Body": body}
        except BaseException:
            storage_access.close_registered(body)
            raise

    def put_object(self, *, Bucket, Key, Body, ContentLength, IfNoneMatch, **fields):
        if IfNoneMatch != "*":
            raise CopyError("target_creation_conflict")
        subject = self._object(Bucket, Key, nonce=fields["Metadata"]["aap-copy-nonce"])
        try:
            return self.channel.upload(
                "migration", "copy_write", subject, Body, length=ContentLength
            )
        except SavedIOError as exc:
            self._error(exc, "copy_write")


@contextmanager
def copy_io(
    *,
    copy_id,
    attempt_id,
    task_id,
    operation_id,
    phase,
    identity,
    revisions,
    timeout=None,
):
    transport = _CopyIO(
        saved_io_client("write", timeout=timeout),
        {
            "copy_id": str(copy_id),
            "attempt_id": str(attempt_id),
            "task_id": task_id,
            "operation_id": str(operation_id),
            "phase": phase,
            "location_revision": revisions["location"],
            "connection_revision": revisions["connection"],
        },
        identity,
    )
    with storage_access.closing_registered(transport) as owned:
        yield owned
