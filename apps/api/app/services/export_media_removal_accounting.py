"""Observe exact-version retirement without granting or changing deletion."""

from contextlib import contextmanager
import threading

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import media_cache_accounting, storage_access
from app.services import storage_usage_accounting as usage
from app.services.conditional_write_accounting import finish as finish
from app.services.export_zip_accounting import immutable, snapshot


class Recorder(media_cache_accounting.Recorder):
    async def _begin(self, key, receipt):
        async with self.factory() as db:
            handle = await usage.begin_in_transaction(
                db,
                endpoint_url=self.endpoint_url,
                bucket=self.bucket,
                object_key=key,
                producer=self.producer,
                owner_id=self.owner_id,
                purpose=self.purpose,
                kind="delete",
                version=receipt["version_id"],
                new_request=True,
            )
            await db.commit()
        return handle


def recorder(db, receipt):
    ref = receipt.storage_ref
    return Recorder(
        async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
        "export_media_removals",
        receipt.id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose="temporary",
    )


class Observation:
    def __init__(self, accounting, client, key, version):
        self.accounting, self.client = accounting, client
        self.key, self.version = key, version
        self.attempt = accounting.begin(key, {"version_id": version})
        self.attempt.evidence.update(final_send_count=0, parameters_observed=False)
        self.before = snapshot(client, accounting.bucket, key)
        self.lock = threading.Lock()
        self.valid, self.opaque, self.sends = False, False, 0

    def parameters(self, params, **kwargs):
        valid = (
            params.get("Bucket") == self.accounting.bucket
            and params.get("Key") == self.key
            and params.get("VersionId") == self.version
            and "VersionId" in params
        )
        with self.lock:
            self.valid = valid
            self.opaque = self.opaque or not valid

    def sending(self, **kwargs):
        with self.lock:
            self.sends += 1
            self.opaque = self.opaque or not self.valid
            self.attempt.evidence.update(
                final_send_count=self.sends, parameters_observed=self.valid
            )

    @contextmanager
    def hooks(self):
        registered = []
        events = getattr(getattr(self.client, "meta", None), "events", None)
        try:
            for name, callback in (
                ("before-parameter-build.s3.DeleteObject", self.parameters),
                ("before-send.s3.DeleteObject", self.sending),
            ):

                def observe(_call=callback, **kwargs):
                    try:
                        _call(**kwargs)
                    except Exception:
                        self.opaque = True
                    return None

                events.register(name, observe)
                registered.append((name, observe))
        except Exception:
            self.opaque = True
        try:
            yield
        finally:
            for name, callback in registered:
                try:
                    events.unregister(name, callback)
                except Exception:
                    self.opaque = True

    def submit(self, call):
        with self.hooks():
            return call()

    def complete(self, response):
        try:
            self._complete(response)
        except Exception:
            # Read/receipt observation must not turn a successful delete into
            # a business failure. Its independent window remains unknown.
            self.attempt.evidence.pop("current_delta_bytes", None)

    def _complete(self, response):
        if not (
            self.valid
            and not self.opaque
            and self.sends == 1
            and immutable(self.version)
            and isinstance(response, dict)
            and response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (200, 204)
            and not response.get("Error")
            and response.get("VersionId") == self.version
            and (
                response.get("DeleteMarker") is None
                or type(response.get("DeleteMarker")) is bool
            )
        ):
            return
        self.attempt.evidence["returned_version"] = response["VersionId"]
        after = snapshot(self.client, self.accounting.bucket, self.key)
        before = self.before
        if before is None or after is None:
            return
        self.attempt.evidence.update(before=before.evidence, after=after.evidence)
        removed = before.versions.get(self.version)
        remaining = {
            version: fact
            for version, fact in before.versions.items()
            if version != self.version
        }
        if removed is not None:
            if (
                after.versions != remaining
                or (removed[0] == "marker" and response.get("DeleteMarker") is not True)
                or (removed[0] == "object" and response.get("DeleteMarker") is True)
            ):
                return
            self.attempt.evidence.update(
                physical_outcome="deleted",
                physical_size_bytes=removed[2] if removed[0] == "object" else 0,
                version_removed=True,
                removed_kind=removed[0],
                current_delta_bytes=after.size - before.size,
            )
        elif after.versions == before.versions and after.current == before.current:
            self.attempt.evidence.update(
                no_removal_observed=True, current_delta_bytes=0
            )
