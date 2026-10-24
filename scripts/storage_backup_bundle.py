"""Private, bounded offline backup generations; resource ownership stays outside."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
import uuid

from worktree_env import BUCKETS

_CHUNK = 1024 * 1024
_HASH = re.compile(r"^[a-f0-9]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_HEADERS = (
    "ContentType",
    "CacheControl",
    "ContentDisposition",
    "ContentEncoding",
    "ContentLanguage",
    "WebsiteRedirectLocation",
)
_OBJECT_FIELDS = {"bucket", "key", "file", "size_bytes", "sha256", "etag", "metadata"}
_MANIFEST_FIELDS = {"schema", "bucket_roles", "database", "objects", "facts", "totals"}


class BackupError(ValueError):
    """A bounded, credential-free diagnostic safe for the command boundary."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code, message):
    raise BackupError(code, message) from None


@dataclass(frozen=True)
class Limits:
    max_objects: int = 10000
    max_total_bytes: int = 512 * 1024**2
    max_object_bytes: int = 64 * 1024**2
    max_dump_bytes: int = 128 * 1024**2

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "max_dump_bytes" else 0):
                _fail("invalid_limits", "Backup limits must be nonnegative integers.")


def _filesystem_errors(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OSError:
            _fail("filesystem_io", "Backup filesystem operation failed.")

    return run


def _json(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("invalid_manifest", "Backup metadata must be finite JSON.")


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _fail("invalid_manifest", "Duplicate JSON fields are not permitted.")
        result[key] = value
    return result


def _parse(value):
    try:
        return json.loads(value, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_manifest", "Backup JSON is not valid.")


def _roles(value):
    if not isinstance(value, dict) or set(value) != set(BUCKETS):
        _fail("invalid_roles", "All nine storage roles must be explicitly mapped.")
    for bucket in value.values():
        if (
            not isinstance(bucket, str)
            or not _BUCKET.fullmatch(bucket)
            or ".." in bucket
            or ".-" in bucket
            or "-." in bucket
        ):
            _fail("invalid_roles", "Storage roles require physical bucket names.")
    return dict(sorted(value.items()))


def _path(value):
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or ".." in Path(raw).parts:
            _fail("unsafe_path", "Backup paths must not traverse parent directories.")
        return Path(os.path.abspath(raw))
    except (TypeError, ValueError):
        _fail("unsafe_path", "Backup path is not a filesystem path.")


@contextmanager
def _directory(value, *, create=False, private=True):
    path = _path(value)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        parts = path.parts[1:]
        if not parts:
            _fail("unsafe_path", "The filesystem root is not a backup generation.")
        for index, part in enumerate(parts):
            if create and index == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                following = os.open(part, flags, dir_fd=descriptor)
            except OSError:
                _fail("unsafe_path", "Backup directories must exist without symlinks.")
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if private and (
            stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()
        ):
            _fail("unsafe_path", "A backup generation must be private and owned.")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _file(directory, name, *, create=False, private=True):
    if not isinstance(name, str) or "/" in name or name in {"", ".", ".."}:
        _fail("unsafe_path", "Backup file names must be direct children.")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY
    try:
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
    except OSError:
        _fail("unsafe_path", "A required regular backup file is missing or unsafe.")
    with os.fdopen(descriptor, "wb" if create else "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (
            private
            and (
                stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            )
        ):
            _fail("unsafe_path", "Backup files must be private regular files.")
        yield stream


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_identity(stream):
    try:
        info = os.fstat(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    return _identity(info) if stat.S_ISREG(info.st_mode) else None


def _copy(stream, output, maximum, *, dump=False):
    before = _stream_identity(stream)
    digest, length, prefix = hashlib.sha256(), 0, b""
    while True:
        try:
            chunk = stream.read(_CHUNK)
        except Exception:
            _fail("storage_io", "A backup input stream could not be read.")
        if not isinstance(chunk, (bytes, bytearray)) or len(chunk) > _CHUNK:
            _fail("storage_io", "A backup input stream returned an invalid chunk.")
        if not chunk:
            break
        length += len(chunk)
        if length > maximum:
            _fail("budget_exceeded", "Backup byte budget exceeded.")
        prefix = (prefix + chunk[:5])[:5]
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if before is not None and _stream_identity(stream) != before:
        _fail("source_changed", "A backup input changed while it was being copied.")
    if dump and prefix != b"PGDMP":
        _fail("dump_integrity", "A PostgreSQL custom-format dump is required.")
    if output is not None:
        output.flush()
        os.fsync(output.fileno())
    return {"size_bytes": length, "sha256": digest.hexdigest()}


def _read(directory, name, maximum):
    with _file(directory, name) as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            _fail("budget_exceeded", "Backup metadata budget exceeded.")
        value = stream.read(maximum + 1)
        if len(value) > maximum:
            _fail("budget_exceeded", "Backup metadata budget exceeded.")
        return value


def _write_json(directory, name, value):
    temporary = ".pending-" + uuid.uuid4().hex
    with _file(directory, temporary, create=True) as stream:
        stream.write(_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def _call(s3, method, **kwargs):
    try:
        result = getattr(s3, method)(**kwargs)
    except Exception:
        _fail("storage_io", f"Storage operation failed: {method}.")
    if not isinstance(result, dict):
        _fail("storage_io", "Storage operation returned an invalid response.")
    return result


def _pages(s3, method, bucket, limits):
    markers, seen = {}, set()
    for _ in range(max(2, limits.max_objects + 2)):
        value = _call(s3, method, Bucket=bucket, **markers)
        yield value
        if not value.get("IsTruncated", False):
            return
        if method == "list_objects_v2":
            markers = {"ContinuationToken": value.get("NextContinuationToken")}
        elif method == "list_object_versions":
            markers = {
                "KeyMarker": value.get("NextKeyMarker"),
                "VersionIdMarker": value.get("NextVersionIdMarker"),
            }
        else:
            markers = {
                "KeyMarker": value.get("NextKeyMarker"),
                "UploadIdMarker": value.get("NextUploadIdMarker"),
            }
        markers = {key: item for key, item in markers.items() if item is not None}
        token = _json(markers)
        if (
            not markers
            or token in seen
            or any(not isinstance(item, str) or not item for item in markers.values())
        ):
            _fail("storage_io", "Storage pagination did not advance safely.")
        seen.add(token)
    _fail("budget_exceeded", "Storage pagination budget exceeded.")


def _current_only(s3, bucket, limits):
    versioning = _call(s3, "get_bucket_versioning", Bucket=bucket)
    if versioning.get("Status") is not None or versioning.get("MFADelete") == "Enabled":
        _fail(
            "unsupported_bucket_state",
            "Versioned storage is outside this backup format.",
        )
    count = 0
    for page in _pages(s3, "list_object_versions", bucket, limits):
        if page.get("DeleteMarkers"):
            _fail(
                "unsupported_bucket_state",
                "Delete markers cannot be omitted from a backup.",
            )
        for item in page.get("Versions", []):
            count += 1
            if (
                item.get("VersionId") not in (None, "null")
                or item.get("IsLatest") is not True
            ):
                _fail(
                    "unsupported_bucket_state",
                    "Historical object versions are not supported.",
                )
            if count > limits.max_objects:
                _fail(
                    "budget_exceeded",
                    "Object version listing exceeded the object budget.",
                )
    for page in _pages(s3, "list_multipart_uploads", bucket, limits):
        if page.get("Uploads"):
            _fail(
                "unsupported_bucket_state",
                "Unfinished multipart uploads prevent backup or restore.",
            )


def _key(value):
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value.encode("utf-8")) <= 1024
            or "\0" in value
        ):
            _fail("invalid_manifest", "Object keys must be valid bounded UTF-8.")
    except UnicodeError:
        _fail("invalid_manifest", "Object keys must be valid bounded UTF-8.")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        _fail(
            "invalid_manifest", "Backup sizes and counts must be nonnegative integers."
        )
    return value


def _etag(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        _fail("invalid_manifest", "Object ETag observations must be present.")
    return value


def _metadata(value, *, frozen=False):
    if frozen and (
        not isinstance(value, dict)
        or set(value) - {*_HEADERS, "Expires", "Metadata"}
        or "Metadata" not in value
        or "ContentType" not in value
    ):
        _fail("invalid_manifest", "Object metadata contains unsupported fields.")
    metadata = value.get("Metadata", {})
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key)
        or not isinstance(item, str)
        or "\r" in item
        or "\n" in item
        for key, item in metadata.items()
    ):
        _fail("invalid_manifest", "Object user metadata must contain strings.")
    result = {
        "Metadata": dict(metadata),
        "ContentType": value.get("ContentType", "application/octet-stream"),
    }
    for name in _HEADERS:
        if name in value:
            result[name] = value[name]
    if any(
        not isinstance(item, str) or "\r" in item or "\n" in item
        for name, item in result.items()
        if name != "Metadata"
    ):
        _fail("invalid_manifest", "Object HTTP metadata must contain safe strings.")
    if "Expires" in value:
        expires = value["Expires"]
        try:
            if frozen:
                expires = datetime.fromisoformat(expires)
            if not isinstance(expires, datetime) or expires.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError):
            _fail("invalid_manifest", "Object expiry metadata must include a timezone.")
        result["Expires"] = expires.astimezone(timezone.utc).isoformat()
    if len(_json(result)) > 16384:
        _fail("budget_exceeded", "Object metadata budget exceeded.")
    return result


def _head(s3, bucket, key):
    value = _call(s3, "head_object", Bucket=bucket, Key=key)
    if value.get("VersionId") not in (None, "null") or value.get("DeleteMarker"):
        _fail(
            "unsupported_bucket_state",
            "Only current unversioned objects can be backed up.",
        )
    if value.get("ObjectLockMode") or value.get("ObjectLockLegalHoldStatus") == "ON":
        _fail(
            "unsupported_bucket_state",
            "Object retention protection requires a separate recovery contract.",
        )
    return {
        "size_bytes": _integer(value.get("ContentLength")),
        "etag": _etag(value.get("ETag")),
        "metadata": _metadata(value),
    }


def _inventory(s3, roles, limits, *, empty=False):
    records, total = {}, 0
    for bucket in sorted(set(roles.values())):
        _current_only(s3, bucket, limits)
        for page in _pages(s3, "list_objects_v2", bucket, limits):
            for value in page.get("Contents", []):
                if empty:
                    _fail(
                        "target_not_empty",
                        "Every restore bucket must be empty before the first write.",
                    )
                key = _key(value.get("Key"))
                identity = (bucket, key)
                if identity in records:
                    _fail(
                        "duplicate_object",
                        "Storage listed one physical object more than once.",
                    )
                size = _integer(value.get("Size"))
                if len(records) >= limits.max_objects or size > limits.max_object_bytes:
                    _fail("budget_exceeded", "Object backup budget exceeded.")
                total += size
                if total > limits.max_total_bytes:
                    _fail("budget_exceeded", "Total object backup budget exceeded.")
                observed = _head(s3, bucket, key)
                if observed["size_bytes"] != size or observed["etag"] != _etag(
                    value.get("ETag")
                ):
                    _fail(
                        "source_changed", "An object changed between listing and HEAD."
                    )
                records[identity] = {"bucket": bucket, "key": key, **observed}
    return [records[key] for key in sorted(records)]


def _object_file(bucket, key):
    identity = hashlib.sha256(_json([bucket, key])).hexdigest()
    return f"objects/{identity}.bin"


def _body(s3, record, output, limits):
    response = _call(
        s3,
        "get_object",
        Bucket=record["bucket"],
        Key=record["key"],
        IfMatch=record["etag"],
    )
    body = response.get("Body")
    if not hasattr(body, "read"):
        _fail("storage_io", "Object content stream is missing.")
    try:
        if (
            _integer(response.get("ContentLength")) != record["size_bytes"]
            or _etag(response.get("ETag")) != record["etag"]
            or _metadata(response) != record["metadata"]
        ):
            _fail(
                "source_changed", "Object metadata changed before its content was read."
            )
        result = _copy(body, output, limits.max_object_bytes)
        if result["size_bytes"] != record["size_bytes"]:
            _fail("source_changed", "Object content length changed during backup.")
        return result
    finally:
        try:
            body.close()
        except Exception:
            pass


@_filesystem_errors
def create_generation(
    directory,
    *,
    s3,
    bucket_roles,
    dump_path,
    facts,
    limits=Limits(),
    before_publish: Callable[[], None] | None = None,
):
    """Seal one private offline dump and complete current-object collection."""
    roles = _roles(bucket_roles)
    frozen_facts = _parse(_json(facts))
    if len(_json(frozen_facts)) > 1024**2:
        _fail("budget_exceeded", "Business evidence metadata budget exceeded.")
    with _directory(directory, create=True) as root:
        if os.listdir(root):
            _fail("generation_not_empty", "A generation directory must be empty.")
        os.mkdir("objects", 0o700, dir_fd=root)
        source_path = _path(dump_path)
        with _directory(source_path.parent, private=False) as parent:
            with _file(parent, source_path.name, private=False) as source:
                if os.fstat(source.fileno()).st_size > limits.max_dump_bytes:
                    _fail("budget_exceeded", "Database dump budget exceeded.")
                with _file(root, "database.dump", create=True) as output:
                    database = {
                        "file": "database.dump",
                        **_copy(source, output, limits.max_dump_bytes, dump=True),
                    }
        before = _inventory(s3, roles, limits)
        objects = []
        with _directory(_path(directory) / "objects") as files:
            for observed in before:
                filename = _object_file(observed["bucket"], observed["key"])
                with _file(files, filename.split("/")[1], create=True) as output:
                    copied = _body(s3, observed, output, limits)
                if _head(s3, observed["bucket"], observed["key"]) != {
                    key: observed[key] for key in ("size_bytes", "etag", "metadata")
                }:
                    _fail(
                        "source_changed",
                        "Object metadata changed after its content was copied.",
                    )
                objects.append({**observed, **copied, "file": filename})
            os.fsync(files)
        if _inventory(s3, roles, limits) != before:
            _fail(
                "source_changed", "The source object collection changed during backup."
            )
        manifest = {
            "schema": 1,
            "bucket_roles": roles,
            "database": database,
            "objects": objects,
            "facts": frozen_facts,
            "totals": {
                "objects": len(objects),
                "object_bytes": sum(item["size_bytes"] for item in objects),
                "dump_bytes": database["size_bytes"],
            },
        }
        if len(_json(manifest)) > _manifest_limit(limits):
            _fail("budget_exceeded", "Generation manifest budget exceeded.")
        _write_json(root, "manifest.json", manifest)
        if before_publish is not None:
            before_publish()
        _write_json(
            root,
            "COMPLETE.json",
            {
                "schema": 1,
                "manifest_sha256": hashlib.sha256(_json(manifest)).hexdigest(),
            },
        )
        return manifest


def _manifest_limit(limits):
    return min(64 * 1024**2, 128 * 1024 + limits.max_objects * 20000)


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("invalid_manifest", "Backup content identities must be SHA-256.")


def _manifest(value, limits):
    if (
        not isinstance(value, dict)
        or set(value) != _MANIFEST_FIELDS
        or type(value["schema"]) is not int
        or value["schema"] != 1
    ):
        _fail("invalid_manifest", "Unsupported generation manifest.")
    roles = _roles(value["bucket_roles"])
    database = value["database"]
    if (
        not isinstance(database, dict)
        or set(database) != {"file", "size_bytes", "sha256"}
        or database["file"] != "database.dump"
    ):
        _fail("invalid_manifest", "The generation dump descriptor is invalid.")
    _digest(database["sha256"])
    if _integer(database["size_bytes"]) > limits.max_dump_bytes:
        _fail("budget_exceeded", "Database dump budget exceeded.")
    objects = value["objects"]
    if not isinstance(objects, list) or len(objects) > limits.max_objects:
        _fail("budget_exceeded", "Generation object count budget exceeded.")
    identities, total = set(), 0
    for item in objects:
        if not isinstance(item, dict) or set(item) != _OBJECT_FIELDS:
            _fail("invalid_manifest", "An object descriptor is invalid.")
        if not isinstance(item["bucket"], str) or item["bucket"] not in set(
            roles.values()
        ):
            _fail("invalid_manifest", "An object names an undeclared physical bucket.")
        key = _key(item["key"])
        identity = (item["bucket"], key)
        if identity in identities:
            _fail("duplicate_object", "A manifest repeats a physical object.")
        identities.add(identity)
        if item["file"] != _object_file(*identity):
            _fail(
                "unsafe_path", "An object file must use its hashed physical identity."
            )
        size = _integer(item["size_bytes"])
        total += size
        if size > limits.max_object_bytes or total > limits.max_total_bytes:
            _fail("budget_exceeded", "Generation object byte budget exceeded.")
        _digest(item["sha256"])
        _etag(item["etag"])
        if _metadata(item["metadata"], frozen=True) != item["metadata"]:
            _fail("invalid_manifest", "Object metadata is not canonical.")
    totals = {
        "objects": len(objects),
        "object_bytes": total,
        "dump_bytes": database["size_bytes"],
    }
    if value["totals"] != totals or any(
        type(item) is not int for item in value["totals"].values()
    ):
        _fail("invalid_manifest", "Generation totals do not match its members.")
    if len(_json(value["facts"])) > 1024**2:
        _fail("budget_exceeded", "Business evidence metadata budget exceeded.")
    return value


@_filesystem_errors
def verify_generation(directory, *, limits=Limits()):
    """Verify the complete generation locally before permitting external writes."""
    with _directory(directory) as root:
        if "COMPLETE.json" not in os.listdir(root):
            _fail("incomplete_generation", "The generation has no completion marker.")
        marker = _parse(_read(root, "COMPLETE.json", 1024))
        raw = _read(root, "manifest.json", _manifest_limit(limits))
        if (
            not isinstance(marker, dict)
            or set(marker) != {"schema", "manifest_sha256"}
            or type(marker["schema"]) is not int
            or marker["schema"] != 1
        ):
            _fail("incomplete_generation", "The completion marker is invalid.")
        if marker["manifest_sha256"] != hashlib.sha256(raw).hexdigest():
            _fail(
                "incomplete_generation",
                "The generation manifest does not match its completion marker.",
            )
        manifest = _manifest(_parse(raw), limits)
        if set(os.listdir(root)) != {
            "COMPLETE.json",
            "manifest.json",
            "database.dump",
            "objects",
        }:
            _fail("invalid_manifest", "Unexpected files exist in the generation.")
        with _file(root, "database.dump") as stream:
            result = _copy(stream, None, limits.max_dump_bytes, dump=True)
            if result != {
                key: manifest["database"][key] for key in ("size_bytes", "sha256")
            }:
                _fail(
                    "dump_integrity",
                    "The retained database dump failed full verification.",
                )
        with _directory(_path(directory) / "objects") as files:
            expected = {item["file"].split("/")[1] for item in manifest["objects"]}
            if set(os.listdir(files)) != expected:
                _fail(
                    "object_integrity",
                    "The retained object collection is incomplete or has unknown files.",
                )
            for item in manifest["objects"]:
                with _file(files, item["file"].split("/")[1]) as stream:
                    result = _copy(stream, None, limits.max_object_bytes)
                if result != {key: item[key] for key in ("size_bytes", "sha256")}:
                    _fail(
                        "object_integrity",
                        "A retained object failed full SHA verification.",
                    )
        return manifest


def _mapping(source, target):
    mapping = {}
    reverse = {}
    for role, source_bucket in source.items():
        target_bucket = target[role]
        if source_bucket in mapping and mapping[source_bucket] != target_bucket:
            _fail(
                "mapping_mismatch",
                "Shared source buckets must remain shared after restore.",
            )
        if target_bucket in reverse and reverse[target_bucket] != source_bucket:
            _fail(
                "mapping_mismatch", "Different source buckets cannot merge at restore."
            )
        if target_bucket in source.values():
            _fail(
                "mapping_mismatch",
                "Restore buckets must be independent from source buckets.",
            )
        mapping[source_bucket] = target_bucket
        reverse[target_bucket] = source_bucket
    return mapping


@_filesystem_errors
def restore_objects(directory, *, s3, bucket_roles, limits=Limits()):
    """Restore only to already owned empty buckets after complete local validation."""
    manifest = verify_generation(directory, limits=limits)
    roles = _roles(bucket_roles)
    mapping = _mapping(manifest["bucket_roles"], roles)
    _inventory(s3, roles, limits, empty=True)
    with _directory(_path(directory) / "objects") as files:
        for item in manifest["objects"]:
            with _file(files, item["file"].split("/")[1]) as stream:
                if _copy(stream, None, limits.max_object_bytes) != {
                    key: item[key] for key in ("size_bytes", "sha256")
                }:
                    _fail(
                        "object_integrity",
                        "A retained object changed after generation verification.",
                    )
                stream.seek(0)
                metadata = dict(item["metadata"])
                if "Expires" in metadata:
                    metadata["Expires"] = datetime.fromisoformat(metadata["Expires"])
                _call(
                    s3,
                    "put_object",
                    Bucket=mapping[item["bucket"]],
                    Key=item["key"],
                    ContentLength=item["size_bytes"],
                    IfNoneMatch="*",
                    # Omit an empty Body: botocore wraps b'' into a stream and
                    # adds Expect/continue, which can stall the next pooled PUT.
                    **({"Body": stream} if item["size_bytes"] else {}),
                    **metadata,
                )
    restored = _inventory(s3, roles, limits)
    expected = {
        (mapping[item["bucket"]], item["key"]): item for item in manifest["objects"]
    }
    if len(restored) != len(expected):
        _fail(
            "object_integrity",
            "The restored object collection does not match the generation.",
        )
    for observed in restored:
        item = expected.get((observed["bucket"], observed["key"]))
        if (
            item is None
            or observed["size_bytes"] != item["size_bytes"]
            or observed["metadata"] != item["metadata"]
        ):
            _fail(
                "metadata_mismatch", "Restored metadata does not match the generation."
            )
        if _body(s3, observed, None, limits)["sha256"] != item["sha256"]:
            _fail("object_integrity", "A restored object failed full SHA verification.")
    return {
        "objects": len(restored),
        "object_bytes": sum(item["size_bytes"] for item in restored),
        "buckets": len(mapping),
        "manifest_sha256": hashlib.sha256(_json(manifest)).hexdigest(),
    }
