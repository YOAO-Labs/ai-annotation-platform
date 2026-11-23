"""One finite packed legacy derivative generation and its retirement."""

import io
import json
import os
import re

from botocore.exceptions import ClientError
from yoao_assets_images.geometry import pyramid_levels
from yoao_assets_source.saved_io import SavedIOError

from .saved_io_media import (
    _FAMILY,
    _denied,
    _digest,
    _fields,
    _identity,
    _mismatch,
    _pin,
    _receipt,
    _uuid,
    _version,
)
from .saved_io_storage import physical_error

_SQL = "yoao_assets_v1.media_io_derivative"
_TILE = re.compile(r"tiles/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp")
_VIDEO = frozenset(
    {
        "metadata.json",
        "poster.webp",
        "playback.mp4",
        "frame.webp",
        "frame.jpeg",
        "chunk.mp4",
    }
)
_MISSING = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})
_MAX_MEMBERS = 20_002


def _member(kind, name):
    return isinstance(name, str) and (
        name in {"manifest.json", "overview.webp"} or bool(_TILE.fullmatch(name))
        if kind == "pyramid"
        else name in _VIDEO
    )


def _unique(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise _mismatch()
    return values


def _decode(kind, body):
    length_bytes = body.read(4)
    if len(length_bytes) != 4:
        raise _mismatch()
    length = int.from_bytes(length_bytes, "big")
    if not 0 < length <= 4 * 1024**2:
        raise _mismatch()
    try:
        inventory = json.loads(body.read(length), object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise _mismatch() from None
    marker = "manifest.json" if kind == "pyramid" else "metadata.json"
    if (
        type(inventory) is not dict
        or marker not in inventory
        or len(inventory) > _MAX_MEMBERS
    ):
        raise _mismatch()
    offset = 4 + length
    offsets = {}
    for member, value in inventory.items():
        if (
            not _member(kind, member)
            or type(value) is not dict
            or set(value) != {"size_bytes", "sha256"}
        ):
            raise _mismatch()
        size, _ = _receipt({"size": value["size_bytes"], "sha256": value["sha256"]})
        if size <= 0:
            raise _mismatch()
        offsets[member] = offset
        offset += size
    if kind == "video" and offset - 4 - length > 2 * 1024**3:
        raise _mismatch()
    if body.seek(0, os.SEEK_END) != offset:
        raise _mismatch()
    cap = 2 * 1024**2 if kind == "pyramid" else 64 * 1024**2
    if inventory[marker]["size_bytes"] > cap:
        raise _mismatch()
    body.seek(offsets[marker])
    try:
        summary = json.loads(
            body.read(inventory[marker]["size_bytes"]), object_pairs_hook=_unique
        )
    except (ValueError, RecursionError):
        raise _mismatch() from None
    if not isinstance(summary, dict):
        raise _mismatch()
    return inventory, offsets, summary


def _verify_members(kind, proof, inventory, summary):
    if kind == "pyramid":
        width, height = summary.get("width"), summary.get("height")
        if (
            summary.get("generation") != proof["generation"]
            or proof.get("source_fingerprint") is not None
            and summary.get("sourceFingerprint") != proof["source_fingerprint"]
            or type(width) is not int
            or type(height) is not int
            or not 0 < min(width, height)
            or max(width, height) > 1_048_576
        ):
            raise _mismatch()
        levels = pyramid_levels(width, height)
        if sum(level["columns"] * level["rows"] for level in levels) + 2 != len(
            inventory
        ):
            raise _mismatch()
        expected = {"manifest.json", "overview.webp"}
        expected.update(
            f"tiles/{level['level']}/{x}/{y}.webp"
            for level in levels
            for x in range(level["columns"])
            for y in range(level["rows"])
        )
    else:
        state = proof["kind"]
        if summary.get("kind") != state:
            raise _mismatch()
        expected = {"metadata.json"}
        if state in ("frame", "chunk") and (
            summary.get("spec") != proof["spec"]
            or summary.get("metadata_output_id") != proof["metadata_output_id"]
        ):
            raise _mismatch()
        if state == "frame":
            expected.add(f"frame.{proof['spec']['format']}")
        elif state == "chunk":
            expected.add("chunk.mp4")
        elif state == "metadata":
            video = summary.get("video")
            if not isinstance(video, dict):
                raise _mismatch()
            for field, relative in (
                ("playback_path", "playback.mp4"),
                ("poster_frame_path", "poster.webp"),
            ):
                value = video.get(field)
                if value is not None:
                    if value != proof["key"] + relative:
                        raise _mismatch()
                    expected.add(relative)
        else:
            raise _mismatch()
    if expected != inventory.keys():
        raise _mismatch()


class _MemberBody(io.RawIOBase):
    def __init__(self, stream, offset, size):
        self._stream, self._offset, self._size = stream, offset, size
        self._position = 0
        self._stream.seek(offset)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._size
        elif whence != io.SEEK_SET:
            raise ValueError("Invalid seek")
        if not 0 <= offset <= self._size:
            raise ValueError("Invalid member offset")
        self._position = offset
        self._stream.seek(self._offset + offset)
        return offset

    def read(self, size=-1):
        remaining = self._size - self._position
        data = self._stream.read(remaining if size < 0 else min(size, remaining))
        self._position += len(data)
        return data


def _listed(client, pin, kind):
    token = None
    objects = {}
    while True:
        page = client.list_objects_v2(
            Bucket=pin.bucket,
            Prefix=pin.key,
            MaxKeys=1000,
            **({"ContinuationToken": token} if token else {}),
        )
        for value in page.get("Contents", []):
            key, size = value.get("Key"), value.get("Size")
            if (
                not isinstance(key, str)
                or not key.startswith(pin.key)
                or key in objects
            ):
                raise _mismatch()
            if (
                not _member(kind, key[len(pin.key) :])
                or type(size) is not int
                or size <= 0
            ):
                raise _mismatch()
            objects[key] = size
            if len(objects) > _MAX_MEMBERS:
                raise _mismatch()
        if page.get("IsTruncated") is False:
            return objects
        new_token = page.get("NextContinuationToken")
        if (
            not isinstance(new_token, str)
            or not new_token
            or new_token == token
            or not page.get("Contents")
        ):
            raise _mismatch()
        token = new_token


def _matched(client, params, receipt, *, etag=None, version=None):
    observed = client.head_object(**params)
    size = receipt["size_bytes"]
    etag, version = _identity(observed, size, etag=etag, version=version)
    response = client.get_object(
        **(
            params
            | {"IfMatch": etag}
            | ({"VersionId": version} if version is not None else {})
        )
    )
    body = response["Body"]
    try:
        if _identity(response, size, etag=etag, version=version) != (etag, version):
            raise _mismatch()
        _digest(body, size, receipt["sha256"])
    finally:
        try:
            body.close()
        except Exception:
            raise SavedIOError(
                "source_close_unsettled", status_code=503, unsettled=True
            ) from None
    if _identity(client.head_object(**params), size, etag=etag, version=version) != (
        etag,
        version,
    ):
        raise _mismatch()
    return observed


def _build(client, pin, kind, body, offsets, inventory):
    if _listed(client, pin, kind):
        raise _mismatch()
    acks = {}
    for relative, receipt in inventory.items():
        key, size = pin.key + relative, receipt["size_bytes"]
        stream = _MemberBody(body, offsets[relative], size)
        _digest(stream, size, receipt["sha256"])
        stream.seek(0)
        suffix = relative.rsplit(".", 1)[-1]
        content_type = {
            "json": "application/json",
            "webp": "image/webp",
            "jpeg": "image/jpeg",
            "mp4": "video/mp4",
        }[suffix]
        try:
            result = client.put_object(
                Bucket=pin.bucket,
                Key=key,
                Body=stream,
                ContentLength=size,
                ContentType=content_type,
                IfNoneMatch="*",
                **(
                    {"CacheControl": "private, max-age=31536000, immutable"}
                    if kind == "pyramid"
                    else {}
                ),
            )
        except Exception as exc:
            raise physical_error(exc, mutation=True) from None
        etag, version = result.get("ETag"), _version(result.get("VersionId"))
        if not etag or result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise SavedIOError("media_write_unproven", status_code=503, unsettled=True)
        acks[relative] = {"etag": etag, "version_id": version}
    expected = {pin.key + name: fact["size_bytes"] for name, fact in inventory.items()}
    if _listed(client, pin, kind) != expected:
        raise _mismatch()
    for relative, receipt in inventory.items():
        ack = acks[relative]
        _matched(
            client,
            {"Bucket": pin.bucket, "Key": pin.key + relative},
            receipt,
            etag=ack["etag"],
            version=ack["version_id"],
        )
        ack["current_head_matches"] = ack["version_id"] is not None
    if _listed(client, pin, kind) != expected:
        raise _mismatch()
    return acks


def _retire(client, pin, kind, proof):
    objects = _listed(client, pin, kind)
    expected = proof["published"]
    if expected is None and proof["inventory"] is not None:
        expected = {pin.key + name: fact for name, fact in proof["inventory"].items()}
    if expected is not None and not objects.keys() <= expected.keys():
        raise _mismatch()
    qualified = {}
    for key, size in objects.items():
        params = {"Bucket": pin.bucket, "Key": key}
        if expected is not None:
            head = _matched(client, params, expected[key])
        else:
            head = client.head_object(**params)
            _identity(head, size)
        qualified[key] = head
    for key, head in qualified.items():
        params = {"Bucket": pin.bucket, "Key": key}
        version = head.get("VersionId")
        if version is not None:
            params["VersionId"] = version
        try:
            client.delete_object(**params, IfMatch=head["ETag"])
            try:
                client.head_object(**params)
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) not in _MISSING:
                    raise
            else:
                raise _mismatch()
        except Exception as exc:
            raise physical_error(exc, mutation=True) from None
    if _listed(client, pin, kind):
        raise SavedIOError("media_cleanup_unproven", status_code=503, unsettled=True)
    return len(qualified)


def _handler(action):
    async def handle(context, subject, body=None):
        _fields(
            subject, "kind", "id", "permit", *(("token",) if action == "build" else ())
        )
        kind = subject["kind"]
        if kind not in ("pyramid", "video"):
            raise _denied()
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])
        token = _uuid(subject["token"]) if action == "build" else None

        async def proof(inventory=None, *, claim=False):
            return await context.fetch(
                _SQL, kind, action, identifier, permit, token, inventory, claim
            )

        expected = await proof()
        pin = await _pin(context, expected)
        if action == "build":
            inventory, offsets, summary = await context.run(_decode, kind, body)
            _verify_members(kind, expected, inventory, summary)
            claimed = await proof(inventory, claim=True)
            if claimed != expected | {"inventory": inventory}:
                raise _denied()
            try:
                async with pin.client() as client:
                    receipts = await context.run(
                        _build, client, pin, kind, body, offsets, inventory
                    )
            except Exception as exc:
                raise physical_error(exc) from None
            if await proof() != claimed:
                raise SavedIOError(
                    "media_owner_changed", status_code=503, unsettled=True
                )
            return {"members": receipts}
        if await proof(claim=True) != expected:
            raise _denied()
        try:
            async with pin.client() as client:
                count = await context.run(_retire, client, pin, kind, expected)
        except Exception as exc:
            raise physical_error(exc) from None
        if await proof() != expected:
            raise SavedIOError(
                "media_cleanup_unproven", status_code=503, unsettled=True
            )
        return {"deleted": count}

    return handle


def register(registry):
    registry.allow(_SQL + "(text,text,uuid,uuid,uuid,jsonb,boolean)")
    registry.add(
        _FAMILY, "derivative-build", "upload", _handler("build"), access="write"
    )
    registry.add(
        _FAMILY, "derivative-delete", "command", _handler("delete"), access="cleanup"
    )
