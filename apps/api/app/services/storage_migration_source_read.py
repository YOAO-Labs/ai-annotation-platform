"""Exact-source read capability; it never owns clients, Bodies, or target writes."""

from urllib.parse import urlsplit, urlunsplit

from app.services.storage_migration_observations import endpoint_identity

INVENTORY_METHODS = frozenset(
    {
        "get_bucket_versioning",
        "get_bucket_lifecycle_configuration",
        "list_objects_v2",
        "list_object_versions",
        "list_multipart_uploads",
        "head_object",
        "get_object",
    }
)
COPY_METHODS = frozenset({"head_object", "get_object"})


class SourceReadError(ValueError):
    code = "migration_source_read_denied"

    def __init__(self):
        super().__init__(self.code)


class SourceRead:
    def __init__(self, client, source, *, owner_kind):
        if owner_kind not in {"inventory", "copy"}:
            raise SourceReadError()
        self._client = client
        self._endpoint = endpoint_identity(source["endpoint"])
        if self._endpoint is None:
            raise SourceReadError()
        self._buckets = frozenset(source["purpose_buckets"].values())
        if not self._buckets or any(
            not isinstance(bucket, str) or not bucket for bucket in self._buckets
        ):
            raise SourceReadError()
        self._methods = INVENTORY_METHODS if owner_kind == "inventory" else COPY_METHODS
        self._check()
        try:
            client.meta.events.register("before-send.s3", self._before_send)
        except Exception:
            raise SourceReadError() from None

    def _before_send(self, request, **kwargs):
        try:
            parsed = urlsplit(request.url)
            actual = endpoint_identity(urlunsplit(parsed._replace(query="")))
        except (TypeError, ValueError):
            actual = None
        if (
            actual is None
            or actual[:3] != self._endpoint[:3]
            or (self._endpoint[3] and not actual[3].startswith(self._endpoint[3] + "/"))
        ):
            raise SourceReadError()

    def _check(self):
        endpoint = getattr(getattr(self._client, "meta", None), "endpoint_url", None)
        if endpoint_identity(endpoint) != self._endpoint:
            raise SourceReadError()

    def __getattr__(self, name):
        if name not in self._methods:
            raise SourceReadError()

        def read(*args, **kwargs):
            self._check()
            if (
                args
                or not isinstance(kwargs.get("Bucket"), str)
                or kwargs["Bucket"] not in self._buckets
            ):
                raise SourceReadError()
            return getattr(self._client, name)(**kwargs)

        return read


async def run(
    db,
    window_id,
    owner_kind,
    owner_id,
    task_id,
    call,
    *args,
    on_acquired=None,
    **kwargs,
):
    """Keep the dedicated permit until the real native thread has drained."""
    from app.services import prediction_inputs, storage_access
    from app.services.storage_migration_windows import acquire_source_read

    permit = await acquire_source_read(
        db,
        window_id=window_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        task_id=task_id,
    )
    result, failure = None, None
    try:
        if on_acquired is not None:
            await on_acquired(permit)
        result = await prediction_inputs.settled_io(call, *args, **kwargs)
    except BaseException as exc:
        failure = exc
    if not getattr(failure, "storage_operation_unsettled", False):
        try:
            await prediction_inputs._settle(permit)
        except BaseException as closing:
            if not permit._settled:
                storage_access.mark_unsettled(closing)
            if failure is None:
                failure = closing
            elif not isinstance(closing, Exception):
                prediction_inputs._uncertainty(closing, failure)
                failure = closing
            else:
                prediction_inputs._uncertainty(failure, closing)
    if failure is not None:
        raise failure
    return result
