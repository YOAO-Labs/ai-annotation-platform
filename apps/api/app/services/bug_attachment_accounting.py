"""Per-call attachment intents with conservative conditional-write evidence."""

from contextlib import contextmanager
import logging
import threading

from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import media_cache_accounting, storage_access

log = logging.getLogger(__name__)


def recorder(db, receipt):
    ref = receipt.storage_ref
    return media_cache_accounting.Recorder(
        async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
        "bug_attachment_uploads",
        receipt.id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose="other_platform",
    )


async def finish(accounting, failure, *, settled=True):
    try:
        await accounting.finish(
            settled=settled
            and not getattr(failure, "storage_operation_unsettled", False)
        )
    except Exception:
        log.warning("Attachment accounting publication deferred")
    except BaseException as publishing:
        if getattr(failure, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(publishing)
        return publishing
    return failure


def immutable(version):
    return (
        isinstance(version, str)
        and 0 < len(version) <= 1024
        and version != "null"
        and not any(ord(char) < 32 or ord(char) == 127 for char in version)
    )


class Observation:
    def __init__(self, accounting, client, key, receipt):
        self.accounting, self.client, self.key = accounting, client, key
        self.attempt = accounting.begin(key, receipt)
        self.attempt.evidence.update(final_send_count=0, condition_observed=False)
        self.lock = threading.Lock()
        self.valid, self.opaque, self.sends, self.clear_ack = False, False, 0, False

    def parameters(self, params, **kwargs):
        valid = (
            params.get("Bucket") == self.accounting.bucket
            and params.get("Key") == self.key
            and params.get("IfNoneMatch") == "*"
        )
        with self.lock:
            self.valid = valid
            self.opaque = self.opaque or not valid

    def sending(self, **kwargs):
        with self.lock:
            self.sends += 1
            self.opaque = self.opaque or not self.valid
            self.attempt.evidence.update(
                final_send_count=self.sends,
                condition_observed=self.valid,
            )

    @property
    def single(self):
        return self.valid and not self.opaque and self.sends == 1

    @contextmanager
    def hooks(self):
        registered = []
        events = getattr(getattr(self.client, "meta", None), "events", None)
        try:
            for name, callback in (
                ("before-parameter-build.s3.PutObject", self.parameters),
                ("before-send.s3.PutObject", self.sending),
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
        failure, response = None, None
        with self.hooks():
            try:
                response = call()
            except BaseException as exc:
                failure = exc
        if failure is not None:
            if (
                self.single
                and isinstance(failure, ClientError)
                and failure.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                == 412
                and failure.response.get("Error", {}).get("Code")
                in {"412", "PreconditionFailed"}
            ):
                self.attempt.evidence.update(
                    physical_outcome="not_written",
                    current_delta_bytes=0,
                )
            raise failure
        self.attempt.written(response)
        self.clear_ack = (
            isinstance(response, dict)
            and response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
            and not response.get("Error")
        )
        return response

    def verified(self, version):
        returned = self.attempt.evidence.get("returned_version")
        if (
            self.single
            and self.clear_ack
            and immutable(returned)
            and version == returned
        ):
            # The existing reader has already enforced this immutable version
            # on HEAD, the complete SHA/size GET and the subsequent HEAD.
            self.attempt.verified(self.client, version)
