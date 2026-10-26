"""Read-only adapters for saved artifacts; logical ownership never changes.

Callers validate the saved owner and business authority. A route admits only one
full logical address/content identity and keeps both publication withdrawal and
the existing owner-specific cleanup purpose visible until physical I/O closes.
Direct S3 URLs retain their original expiry contract after signing.
"""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import re
import uuid

from app.services import (
    storage_access,
    storage_migration_copy as copy,
    storage_migration_read_publications as publications,
)
from app.services.content_snapshot import ContentError


async def resolve(
    db,
    ref,
    object_key,
    sha256,
    size_bytes=None,
    *,
    source_version_id=publications.ANY_VERSION,
    source_etag=None,
):
    """Look up saved content only; missing evidence grants no migration route."""
    if not isinstance(sha256, str) or re.fullmatch(r"[a-f0-9]{64}", sha256) is None:
        return None
    if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
        return None
    if source_etag is not None and (
        not isinstance(source_etag, str) or not 0 < len(source_etag) <= 1024
    ):
        return None
    return await publications.lookup(
        db,
        source_location_id=uuid.UUID(ref["location_id"]),
        endpoint_url=ref["endpoint_url"],
        bucket=ref["bucket"],
        object_key=object_key,
        sha256=sha256,
        size_bytes=size_bytes,
        source_version_id=source_version_id,
        **({"source_etag": source_etag} if source_etag is not None else {}),
    )


def reject():
    raise ContentError("read_publication_changed", 409)


def validate_response(
    route, response, *, status_code=200, size_bytes=None, content_range=None
):
    """Check exact copied headers; the caller owns the response body's close.

    Ranged transports supply their selected length and Content-Range. The saved
    full content identity remains established by sealed copy verification and
    a full HEAD, rather than treating a partial GET as a full SHA calculation.
    """
    if not isinstance(response, dict):
        reject()
    metadata = response.get("Metadata", {})
    if not isinstance(metadata, dict):
        reject()
    try:
        content_sha256 = copy.digest(copy.content_target(response))
    except (copy.CopyError, TypeError, AttributeError):
        reject()
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != status_code
        or response.get("VersionId") != route.target_version_id
        or response.get("ETag") != route.target_etag
        or type(response.get("ContentLength")) is not int
        or response.get("ContentLength")
        != (route.size_bytes if size_bytes is None else size_bytes)
        or response.get("ContentRange") != content_range
        or response.get("DeleteMarker")
        or metadata.get("aap-copy-plan") != route.creation_plan_uuid
        or metadata.get("aap-copy-object") != route.source_object_id
        or metadata.get("aap-copy-nonce") != route.creation_nonce
        or content_sha256 != route.target_content_sha256
    ):
        reject()


@dataclass(frozen=True)
class Admission:
    route: object
    target: object
    permit: object


@asynccontextmanager
async def admit(db, route, purpose, *, on_acquired=None):
    """Keep both actual-target permits around caller-owned closed read work.

    This opens no SDK and commits no caller transaction. The caller finishes
    metadata transactions before I/O and drains its existing physical boundary
    before leaving, including body/client close failures and cancellation.
    """
    target = await publications.target_for_route(db, route)
    async with await storage_access.acquire(
        db,
        route.target_location_id,
        route.purpose,
        expected_revision=route.maintenance_revision,
    ):
        async with await storage_access.acquire(
            db,
            route.target_location_id,
            purpose,
            expected_revision=route.maintenance_revision,
        ) as permit:
            if on_acquired is not None:
                await on_acquired(permit)
            await publications.recheck(db, route)
            yield Admission(route, target, permit)
            await publications.recheck(db, route)


class _ReadBody:
    """Track physical close without closing a caller-closed body twice."""

    def __init__(self, body):
        self._body, self._closed = body, False

    def __getattr__(self, name):
        return getattr(self._body, name)

    def close(self):
        if not self._closed:
            storage_access.close_registered(self._body)
            self._closed = True


class ReadClient:
    """A single-object, full-read facade; writes and listings are unavailable."""

    def __init__(self, client, route):
        self._client, self.route = client, route
        self._bodies = []

    @property
    def meta(self):
        return self._client.meta

    def _params(self, values):
        route = self.route
        if (
            values.get("Bucket") != route.source_bucket
            or values.get("Key") != route.source_object_key
            or (
                "VersionId" in values
                and values["VersionId"]
                not in (route.source_version_id, route.target_version_id)
            )
            or "Range" in values
            or "PartNumber" in values
        ):
            reject()
        if "IfMatch" in values and values["IfMatch"] not in (
            route.source_etag,
            route.target_etag,
        ):
            reject()
        return {
            **values,
            "Bucket": route.target_bucket,
            "Key": route.target_object_key,
            "VersionId": route.target_version_id,
            **({"IfMatch": route.target_etag} if "IfMatch" in values else {}),
        }

    def _response(self, response):
        try:
            metadata = response.get("Metadata", {})
            validate_response(self.route, response)
            # Keep strict business metadata comparisons intact. Only these
            # independently verified migration creation fields are transport facts.
            result = {
                **response,
                "Metadata": {
                    key: value
                    for key, value in metadata.items()
                    if key.lower() not in copy.RESERVED
                },
            }
            if response.get("Body") is not None:
                body = _ReadBody(response["Body"])
                self._bodies.append(body)
                result["Body"] = body
            return result
        except BaseException:
            if response.get("Body") is not None:
                storage_access.close_registered(response["Body"])
            raise

    def head_object(self, **kwargs):
        return self._response(self._client.head_object(**self._params(kwargs)))

    def get_object(self, **kwargs):
        params = self._params(kwargs)
        if "IfMatch" in params and params["IfMatch"] != self.route.target_etag:
            reject()
        params["IfMatch"] = self.route.target_etag
        return self._response(self._client.get_object(**params))

    def generate_presigned_url(self, operation, *, Params, ExpiresIn, **kwargs):
        if operation != "get_object" or kwargs:
            reject()
        return self._client.generate_presigned_url(
            operation, Params=self._params(Params), ExpiresIn=ExpiresIn
        )

    def close(self):
        failure = None
        for body in self._bodies:
            try:
                body.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure


@contextmanager
def client(route, target):
    """Open only under the route's actual durable permits."""
    with target.client() as physical:
        reader = ReadClient(physical, route)
        try:
            yield reader
        finally:
            reader.close()


class ReadClientSet:
    """An explicit consumed-member set on one published physical transport."""

    def __init__(self, physical, routes):
        self._client = physical
        self._readers = {}
        context = None
        for route in routes:
            current = (
                route.publication_id,
                route.publication_revision,
                route.source_location_id,
                route.source_endpoint_url,
                route.source_bucket,
                route.target_location_id,
                route.target_location_revision,
                route.target_connection_id,
                route.target_connection_revision,
                route.target_endpoint_url,
                route.target_bucket,
                route.maintenance_revision,
            )
            key = (route.source_bucket, route.source_object_key)
            if (context is not None and current != context) or key in self._readers:
                reject()
            context = current
            self._readers[key] = ReadClient(physical, route)
        if not self._readers:
            reject()

    @property
    def meta(self):
        return self._client.meta

    def reader_for(self, bucket, key):
        reader = self._readers.get((bucket, key))
        if reader is None:
            reject()
        return reader

    def head_object(self, **kwargs):
        return self.reader_for(kwargs.get("Bucket"), kwargs.get("Key")).head_object(
            **kwargs
        )

    def get_object(self, **kwargs):
        return self.reader_for(kwargs.get("Bucket"), kwargs.get("Key")).get_object(
            **kwargs
        )

    def generate_presigned_url(self, operation, *, Params, ExpiresIn, **kwargs):
        return self.reader_for(
            Params.get("Bucket"), Params.get("Key")
        ).generate_presigned_url(
            operation, Params=Params, ExpiresIn=ExpiresIn, **kwargs
        )

    def close(self):
        failure = None
        for reader in self._readers.values():
            try:
                reader.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure


@contextmanager
def clients(routes, target):
    """Open the explicit set inside caller-owned admission and drain boundary."""
    with target.client() as physical:
        readers = ReadClientSet(physical, routes)
        try:
            yield readers
        finally:
            readers.close()


async def run(
    db,
    route,
    purpose,
    callback,
    *,
    on_acquired=None,
    close_result=None,
    timeout=None,
):
    """Run a closed synchronous read, rechecking before any result is delivered.

    The callback receives a ReadClient, closes response bodies itself, and may
    return local staged files. close_result owns those files if cancellation or
    the final route check prevents delivery. Both permits stay on the physical
    target; no exception to the stopped source Gate is introduced.
    """
    target = await publications.target_for_route(db, route)
    result, completed = None, False

    def physical():
        nonlocal result, completed
        with client(route, target) as reader:
            result = callback(reader)
            completed = True
        return result

    async def acquired(permit):
        if on_acquired is not None:
            await on_acquired(permit)
        await publications.recheck(db, route)

    try:
        async with await storage_access.acquire(
            db,
            route.target_location_id,
            route.purpose,
            expected_revision=route.maintenance_revision,
        ):
            await storage_access.run(
                db,
                route.target_location_id,
                purpose,
                physical,
                expected_revision=route.maintenance_revision,
                timeout=timeout,
                on_acquired=acquired,
            )
            await publications.recheck(db, route)
            return result
    except BaseException:
        if completed and close_result is not None:
            close_result(result)
        raise
