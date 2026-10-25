"""Private durable state for an explicitly owned native source fence.

The adapter owns physical identity, process and configuration checks. This module
never interprets a saved observation as permission to reopen a source.
"""

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid

SCHEMA = "aap.storage-source-fence/v1"
CLAIM_SCHEMA = "aap.storage-migration-fence-claim/v1"
MAX_BYTES = 4 * 1024**2
MAX_RECEIPT_BYTES = 16 * 1024
MAX_COUNTER = 2**63 - 1
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = frozenset(
    {"open", "closing", "sealing", "held", "uncertain", "releasing", "released"}
)


class FenceError(ValueError):
    def __init__(self, code):
        self.code = (
            code if isinstance(code, str) and _CODE.fullmatch(code) else "fence_error"
        )
        super().__init__(self.code)


def _identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise FenceError("operation_id_invalid") from None
    return value


def _json(value, *, maximum=MAX_BYTES):
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise FenceError("journal_invalid") from None
    if len(encoded) + 1 > maximum:
        raise FenceError("journal_budget")
    return encoded + b"\n"


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise FenceError("journal_invalid")
        result[key] = value
    return result


def _counts(value):
    if (
        not isinstance(value, dict)
        or len(value) > 64
        or any(
            not isinstance(key, str)
            or not _CODE.fullmatch(key)
            or type(number) is not int
            or not 0 <= number <= MAX_COUNTER
            for key, number in value.items()
        )
    ):
        raise FenceError("observation_invalid")
    return dict(value)


def _observation(value):
    if not isinstance(value, dict) or set(value) != {"observed_at", "counts"}:
        raise FenceError("observation_invalid")
    stamp = value["observed_at"]
    try:
        if not isinstance(stamp, str) or len(stamp) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise FenceError("observation_invalid") from None
    return {"observed_at": stamp, "counts": _counts(value["counts"])}


def _application_claim(value, config_digest, operation_id):
    fields = {
        "schema",
        "finalization_id",
        "window_id",
        "expected_window_revision",
        "database_identity",
        "reservation_nonce",
        "native_operation_id",
        "config_digest",
        "reserved",
    }
    try:
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != CLAIM_SCHEMA
            or type(value["expected_window_revision"]) is not int
            or not 1 <= value["expected_window_revision"] <= MAX_COUNTER
            or type(value["reserved"]) is not bool
            or any(
                not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key])
                for key in ("database_identity", "config_digest")
            )
            or value["config_digest"] != config_digest
            or value["native_operation_id"] != operation_id
        ):
            raise ValueError
        for key in (
            "finalization_id",
            "window_id",
            "reservation_nonce",
            "native_operation_id",
        ):
            _identifier(value[key])
    except (ValueError, TypeError, KeyError):
        raise FenceError("application_claim_invalid") from None
    return deepcopy(value)


def _private(info, mode, *, directory=False):
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise FenceError("journal_permissions")


class Journal:
    """Hold a nonblocking, separate lock across atomic journal replacements."""

    def __init__(self, path: Path, config_digest: str, create=False):
        self.path = Path(os.path.abspath(path))
        if not isinstance(config_digest, str) or not _DIGEST.fullmatch(config_digest):
            raise FenceError("config_digest_invalid")
        self.config_digest, self.create = config_digest, create
        self.data = None
        self._directory = self._lock = None
        self._saved = None
        self._revision = None

    def __enter__(self):
        if self._directory is not None:
            raise FenceError("journal_already_open")
        try:
            # Do not follow a symlink even in an ancestor of the private directory.
            for component in self.path.parent.parents:
                if stat.S_ISLNK(component.lstat().st_mode):
                    raise FenceError("journal_permissions")
            if self.create:
                try:
                    self.path.parent.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            self._directory = os.open(
                self.path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            _private(os.fstat(self._directory), 0o700, directory=True)
            self._lock = os.open(
                self.path.name + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory,
            )
            _private(os.fstat(self._lock), 0o600)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FenceError("journal_busy") from None
            if self.create:
                self.data = {
                    "schema": SCHEMA,
                    "config_digest": self.config_digest,
                    "revision": 1,
                    "phase": "open",
                    "operation_id": None,
                    "snapshot": None,
                    "barriers": [],
                    "observation": None,
                }
                self._validate(self.data)
                self._write(_json(self.data), initial=True)
            else:
                self._saved = self._read()
                self.data = json.loads(
                    self._saved,
                    object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        FenceError("journal_invalid")
                    ),
                )
                self._validate(self.data)
            self._revision = self.data["revision"]
            return self
        except BaseException as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, FenceError) or not isinstance(exc, Exception):
                raise
            raise FenceError("journal_unavailable") from None

    def __exit__(self, *_):
        for name in ("_lock", "_directory"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                finally:
                    setattr(self, name, None)

    def _validate(self, value):
        if (
            not isinstance(value, dict)
            or value.get("schema") != SCHEMA
            or value.get("config_digest") != self.config_digest
            or type(value.get("revision")) is not int
            or not 1 <= value["revision"] <= MAX_COUNTER
            or not isinstance(value.get("phase"), str)
            or value["phase"] not in _PHASES
            or not {"operation_id", "snapshot", "barriers", "observation"}
            <= value.keys()
            or (
                value["snapshot"] is not None
                and not isinstance(value["snapshot"], dict)
            )
            or not isinstance(value["barriers"], list)
            or len(value["barriers"]) > 128
        ):
            raise FenceError("journal_invalid")
        if value["operation_id"] is not None:
            _identifier(value["operation_id"])
        if value.get("release_operation_id") is not None:
            _identifier(value["release_operation_id"])
        if "application_claim" in value:
            _application_claim(
                value["application_claim"], self.config_digest, value["operation_id"]
            )
        for receipt in value["barriers"]:
            if not isinstance(receipt, dict):
                raise FenceError("journal_invalid")
            _json(receipt, maximum=MAX_RECEIPT_BYTES)
        if value["observation"] is not None:
            _observation(value["observation"])
        if value["phase"] == "held" and (
            value["operation_id"] is None
            or value["snapshot"] is None
            or len(value["barriers"]) < 2
            or value["observation"] is None
            or value.get("release_operation_id") is not None
        ):
            raise FenceError("journal_invalid")
        if value["phase"] != "held" and value["observation"] is not None:
            raise FenceError("journal_invalid")
        if value["phase"] in {"releasing", "released"} and not value.get(
            "release_operation_id"
        ):
            raise FenceError("journal_invalid")
        if (
            type(value.get("aborted_uploads", 0)) is not int
            or not 0 <= value.get("aborted_uploads", 0) <= MAX_COUNTER
        ):
            raise FenceError("journal_invalid")

    def _read(self):
        descriptor = os.open(
            self.path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self._directory,
        )
        try:
            info = os.fstat(descriptor)
            _private(info, 0o600)
            if info.st_size > MAX_BYTES:
                raise FenceError("journal_budget")
            chunks, size = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FenceError("journal_budget")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _write(self, encoded, *, initial=False):
        temporary = "." + self.path.name + "." + uuid.uuid4().hex + ".tmp"
        descriptor = None
        try:
            if initial:
                try:
                    os.stat(
                        self.path.name, dir_fd=self._directory, follow_symlinks=False
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise FenceError("journal_exists")
            elif self._read() != self._saved:
                raise FenceError("journal_changed")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory,
            )
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise FenceError("journal_write_failed")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                self.path.name,
                src_dir_fd=self._directory,
                dst_dir_fd=self._directory,
            )
            os.fsync(self._directory)
            self._saved = encoded
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._directory)
            except FileNotFoundError:
                pass

    def save(self):
        if self._directory is None or self.data is None:
            raise FenceError("journal_not_open")
        if self.data.get("revision") != self._revision or self._revision >= MAX_COUNTER:
            raise FenceError("journal_revision_invalid")
        candidate = deepcopy(self.data)
        candidate["revision"] = self._revision + 1
        self._validate(candidate)
        encoded = _json(candidate)
        try:
            self._write(encoded)
        except OSError:
            raise FenceError("journal_write_failed") from None
        self.data = candidate
        self._revision = candidate["revision"]

    def bind_application(self, claim):
        claim = _application_claim(claim, self.config_digest, self.data["operation_id"])
        if self.data["phase"] != "held":
            raise FenceError("application_fence_not_held")
        existing = self.data.get("application_claim")
        if existing is not None:
            if {k: v for k, v in existing.items() if k != "reserved"} != {
                k: v for k, v in claim.items() if k != "reserved"
            }:
                raise FenceError("application_claim_conflict")
            return
        # A failed write cannot grant permission to reopen in this process.
        self.data["application_claim"] = claim
        self.save()

    def _matching_application(self, finalization_id, reservation_nonce):
        try:
            _identifier(finalization_id)
            _identifier(reservation_nonce)
        except FenceError:
            raise FenceError("application_claim_invalid") from None
        claim = self.data.get("application_claim")
        if claim is None or (
            claim["finalization_id"] != finalization_id
            or claim["reservation_nonce"] != reservation_nonce
        ):
            raise FenceError("application_claim_conflict")
        return claim

    def mark_application_reserved(self, finalization_id, reservation_nonce):
        claim = self._matching_application(finalization_id, reservation_nonce)
        if not claim["reserved"]:
            claim["reserved"] = True
            self.save()

    def unbind_application(self, finalization_id, reservation_nonce):
        claim = self._matching_application(finalization_id, reservation_nonce)
        del self.data["application_claim"]
        try:
            self.save()
        except BaseException:
            self.data["application_claim"] = claim
            raise


class Controller:
    def __init__(self, journal, adapter):
        self.journal, self.adapter = journal, adapter

    def _cas(self, expected_revision):
        if (
            type(expected_revision) is not int
            or expected_revision != self.journal.data["revision"]
        ):
            raise FenceError("revision_changed")

    @staticmethod
    def _native(call, *args):
        try:
            return call(*args)
        except FenceError:
            raise
        except Exception:
            raise FenceError("native_operation_failed") from None

    def preview(self):
        try:
            return _counts(self._native(self.adapter.preview))
        except BaseException as exc:
            if self.journal.data["phase"] == "held":
                self._uncertain(exc)
            raise

    def _intent(self, phase, step):
        self.journal.data.update(phase=phase, observation=None, intent={"step": step})
        self.journal.save()

    def _uncertain(self, failure):
        # Retain only acknowledged journal facts. An oversized/unserializable
        # new receipt must not prevent saving uncertainty about its side effect.
        self.journal.data = json.loads(self.journal._saved)
        self.journal.data.update(phase="uncertain", observation=None)
        try:
            self.journal.save()
        except Exception:
            # A failed journal cannot grant proof. Preserve cancellation even if
            # the durable invalidation itself cannot be acknowledged.
            if isinstance(failure, Exception):
                raise FenceError("journal_write_failed") from None

    def _summary(self, observation=None):
        row = self.journal.data
        observed = observation or row["observation"]
        counts = dict(observed["counts"]) if observed else {}
        if row.get("aborted_uploads") is not None:
            counts["aborted_uploads"] = row["aborted_uploads"]
        return {
            "schema": SCHEMA,
            "revision": row["revision"],
            "phase": row["phase"],
            "held": row["phase"] == "held" and observed is not None,
            "observed_at": observed["observed_at"]
            if observed
            else row.get("released_at")
            if row["phase"] == "released"
            else None,
            "counts": counts,
        }

    def _verify(self):
        row = self.journal.data
        return _observation(
            self._native(
                self.adapter.verify,
                deepcopy(row["snapshot"]),
                deepcopy(row["barriers"]),
            )
        )

    def status(self):
        if self.journal.data["phase"] != "held":
            return self._summary()
        try:
            observed = self._verify()
        except BaseException as exc:
            self._uncertain(exc)
            raise
        return self._summary(observed)

    def seal(self, expected_revision: int, operation_id: str):
        if "application_claim" in self.journal.data:
            raise FenceError("application_fence_bound")
        self._cas(expected_revision)
        _identifier(operation_id)
        row = self.journal.data
        new = row["phase"] in {"open", "released"}
        if not new and (
            row["operation_id"] != operation_id
            or row.get("release_operation_id") is not None
        ):
            raise FenceError("operation_conflict")
        if row["phase"] == "held":
            return self.status()
        if new:
            if operation_id in {row["operation_id"], row.get("release_operation_id")}:
                raise FenceError("operation_conflict")
            self.preview()
            row.update(
                operation_id=operation_id,
                release_operation_id=None,
                snapshot=None,
                barriers=[],
                aborted_uploads=0,
                released_at=None,
            )
        try:
            self._intent("closing", "readonly")
            self._native(self.adapter.readonly)
            if self.journal.data["snapshot"] is None:
                self._intent("closing", "prepare")
                snapshot = self._native(self.adapter.prepare)
                if not isinstance(snapshot, dict):
                    raise FenceError("observation_invalid")
                _json(snapshot)
                self.journal.data["snapshot"] = deepcopy(snapshot)
                self.journal.save()
            self._intent("sealing", "disable_lifecycle")
            self._native(
                self.adapter.disable_lifecycle, deepcopy(self.journal.data["snapshot"])
            )
            self._barrier("barrier_before_abort")
            self._intent("sealing", "abort_uploads")
            aborted = self._native(self.adapter.abort_uploads)
            previous_aborts = self.journal.data.get("aborted_uploads", 0)
            if (
                type(aborted) is not int
                or not 0 <= aborted <= MAX_COUNTER - previous_aborts
            ):
                raise FenceError("observation_invalid")
            self.journal.data["aborted_uploads"] = previous_aborts + aborted
            self.journal.save()
            self._barrier("barrier_after_abort")
            self._intent("sealing", "verify")
            observed = self._verify()
            self.journal.data.update(phase="held", observation=observed, intent=None)
            self.journal.save()
        except BaseException as exc:
            self._uncertain(exc)
            raise
        return self._summary()

    def _barrier(self, step):
        self._intent("sealing", step)
        if len(self.journal.data["barriers"]) >= 128:
            raise FenceError("journal_budget")
        receipt = self._native(self.adapter.barrier)
        if not isinstance(receipt, dict):
            raise FenceError("observation_invalid")
        _json(receipt, maximum=MAX_RECEIPT_BYTES)
        self.journal.data["barriers"].append(deepcopy(receipt))
        self.journal.save()

    def release(self, expected_revision: int, operation_id: str):
        if "application_claim" in self.journal.data:
            raise FenceError("application_fence_bound")
        self._cas(expected_revision)
        _identifier(operation_id)
        row = self.journal.data
        existing = row.get("release_operation_id")
        if operation_id == row["operation_id"] or (
            existing is not None and existing != operation_id
        ):
            raise FenceError("operation_conflict")
        if row["phase"] == "released":
            try:
                self._native(self.adapter.verify_open, deepcopy(row["snapshot"]))
            except BaseException as exc:
                self._uncertain(exc)
                raise
            return self._summary()
        try:
            row["release_operation_id"] = operation_id
            self._intent("releasing", "restore_lifecycle")
            if self.journal.data["snapshot"] is not None:
                self._native(
                    self.adapter.restore_lifecycle,
                    deepcopy(self.journal.data["snapshot"]),
                )
            self._intent("releasing", "writable")
            self._native(self.adapter.writable)
            self._intent("releasing", "verify_open")
            self._native(
                self.adapter.verify_open, deepcopy(self.journal.data["snapshot"])
            )
            self.journal.data.update(
                phase="released",
                intent=None,
                released_at=datetime.now(timezone.utc).isoformat(),
            )
            self.journal.save()
        except BaseException as exc:
            self._uncertain(exc)
            raise
        return self._summary()
