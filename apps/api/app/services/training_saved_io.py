"""Synchronous Source operations for a physically admitted training owner."""

from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from botocore.exceptions import ClientError
from yoao_assets_source.saved_io import SavedIOError

from app.services import storage_access
from app.services.saved_io import saved_io_client
from app.services.training_dataset_bridge import TrainingDatasetError

_SCOPES = {
    "training-dataset": "dataset",
    "training-run": "run",
    "training-preview": "preview",
    "sample-evaluation": "evaluation",
    "active-learning": "learning",
}
_MISSING = {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


@contextmanager
def _product_errors(action):
    try:
        yield
    except SavedIOError as exc:
        if exc.storage_code in _MISSING and not exc.unsettled:
            raise ClientError(
                {
                    "Error": {"Code": exc.storage_code},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                action,
            ) from None
        error = TrainingDatasetError(
            "storage_operation_unsettled" if exc.unsettled else exc.code,
            "训练存储来源不可用或对象身份已变化",
            status_code=503 if exc.unsettled else exc.status_code,
        )
        if exc.unsettled:
            storage_access.mark_unsettled(error)
        raise error from None


class _Body:
    def __init__(self, stream):
        self._stream = stream
        with _product_errors("download"):
            self._response = stream.__enter__()

    @property
    def metadata(self):
        return self._response.metadata

    def read(self, length=-1):
        with _product_errors("download"):
            return self._response.read(length)

    def iter_chunks(self, chunk_size):
        while value := self.read(chunk_size):
            yield value

    def close(self):
        with _product_errors("download"):
            self._stream.__exit__(None, None, None)


@dataclass(frozen=True)
class TrainingSavedIO:
    owner: object
    permit: object
    attempt: UUID | None = None
    deletion: UUID | None = None
    read_key: str | None = None
    accounting: object = None
    timeout: float | None = None

    @property
    def training_bucket(self):
        return self.owner.bucket

    def _subject(self, key=None, *, evidence=None):
        scope = _SCOPES[self.owner.ref["purpose"]]
        owner_id = self.owner.logical_prefix.split("/")[3]
        attempt = self.attempt
        member = None
        if key is not None:
            self.owner.contains(key)
            if self.read_key is not None and self.read_key != key:
                raise ValueError("A training read is bound to one artifact")
            relative = key[len(self.owner.prefix) :]
            if scope == "dataset":
                if relative.startswith("media/"):
                    member = "media:" + str(UUID(relative[6:]))
                elif relative.startswith("masks/") and relative.endswith(".json"):
                    member = "mask:" + str(UUID(relative[6:-5]))
                else:
                    directory, aid, name = relative.split("/", 2)
                    if directory != "attempts" or name != "dataset.zip":
                        raise ValueError("Not a training dataset archive")
                    attempt, member = UUID(aid), "archive"
            elif scope == "run":
                directory, aid, member = relative.split("/", 2)
                if directory != "attempts" or (
                    attempt is not None and attempt != UUID(aid)
                ):
                    raise ValueError("Not this training attempt's artifact")
                attempt = UUID(aid)
            elif scope == "evaluation":
                aid, name = relative.split("/", 1)
                if not name.endswith(".json") or (
                    attempt is not None and attempt != UUID(aid)
                ):
                    raise ValueError("Not this evaluation attempt's report")
                attempt, member = UUID(aid), name[:-5]
            elif scope == "learning":
                if not relative.startswith("media/") or not relative.endswith(".png"):
                    raise ValueError("Not a retained learning sample")
                member = relative[6:-4]
            else:
                member = relative
        if self.deletion is not None:
            scope, owner_id = "deletion", str(self.deletion)
        return {
            "permit": str(self.permit.id),
            "scope": scope,
            "owner": owner_id,
            "attempt": str(attempt) if attempt is not None else None,
            "member": member,
            "evidence": evidence or {},
        }

    def _command(self, access, action, subject):
        with _product_errors(action):
            return saved_io_client(access, timeout=self.timeout).command(
                "training", action, subject
            )

    def head(self, key):
        return self._command("read", "head", self._subject(key))

    def download(self, key, *, if_match=None):
        client = saved_io_client("read", timeout=self.timeout)
        body = _Body(
            client.download(
                "training",
                "download",
                self._subject(key, evidence={"if_match": if_match}),
            )
        )
        return {**body.metadata, "Body": body}

    def sign_download(self, key, *, expires_in=3600, download_name=None):
        return self._command(
            "read",
            "sign_download",
            self._subject(
                key,
                evidence={
                    "expires_in": expires_in,
                    "download_name": download_name,
                },
            ),
        )["url"]

    def sign_upload(self, key, *, expires_in):
        return self._command(
            "write",
            "sign_upload",
            self._subject(key, evidence={"expires_in": expires_in}),
        )

    def upload(self, key, body, *, sha256, size_bytes, content_type, multipart=False):
        evidence = {"sha256": sha256, "size_bytes": size_bytes}
        mutation = (
            self.accounting.begin(key, evidence)
            if self.accounting is not None
            else None
        )
        subject = self._subject(
            key,
            evidence={**evidence, "content_type": content_type, "multipart": multipart},
        )
        client = saved_io_client("write", timeout=self.timeout)
        with _product_errors("upload"):
            reply = client.upload(
                "training", "upload", subject, body, length=size_bytes
            )
        if mutation is not None:
            if reply["created"]:
                mutation.written(reply["receipt"])
                if reply["current"]:
                    mutation.evidence.update(
                        full_readback=True,
                        current_head_matches=True,
                        current_delta_bytes=size_bytes,
                    )
            else:
                mutation.evidence.update(
                    physical_outcome="not_written", current_delta_bytes=0
                )

    def retire(self, prefix):
        expected = self.owner.prefix
        if self.attempt is not None:
            expected += f"attempts/{self.attempt}/"
        if prefix != expected:
            raise ValueError("Not this training owner's retirement prefix")
        return self._command("cleanup", "retire", self._subject())["deleted"]

    def discard_report(self, key):
        self._command("cleanup", "discard_report", self._subject(key))
