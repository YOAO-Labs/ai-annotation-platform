"""Exact read-only provider operations for Source-owned directory references."""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import re
import socket
import stat
import time
from urllib.parse import unquote_plus

import paramiko
from botocore.exceptions import BotoCoreError, ClientError

from yoao_assets_source.connectors.host_guard import (
    ConnectorHostDenied,
    resolve_allowed_ips,
)
from yoao_assets_source.connectors.s3_socket import open_pinned_s3
from yoao_assets_source.connectors.sftp import load_sftp_private_key
from yoao_assets_source.reference_protocol import (
    ReferenceError,
    s3_anchor,
    s3_directory_prefix,
    sftp_anchor,
    sftp_relative_path,
)

PAGE_SIZE = 200
MAX_DIRECTORY_ENTRIES = 5000
_INVALID_ETAG = '"aap-impossible-etag"'


@contextmanager
def _closing(handle):
    try:
        yield handle
    finally:
        try:
            handle.close()
        except Exception:
            raise ReferenceError(
                "storage_operation_unsettled", 503, unsettled=True
            ) from None


def _s3_key(value):
    if not isinstance(value, str) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ReferenceError("s3_invalid_listing")
    try:
        result = unquote_plus(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise ReferenceError("s3_invalid_listing") from None
    if "\x00" in result or len(result.encode()) > 1024:
        raise ReferenceError("s3_invalid_key")
    return result


def _s3_observation(response, bucket, key):
    etag, modified, size, version = (
        response.get("ETag"),
        response.get("LastModified"),
        response.get("ContentLength"),
        response.get("VersionId"),
    )
    if (
        not isinstance(etag, str)
        or not etag
        or len(etag) > 256
        or type(size) is not int
        or size < 0
        or not isinstance(modified, datetime)
        or (
            version is not None
            and (not isinstance(version, str) or not 0 < len(version) <= 1024)
        )
    ):
        raise ReferenceError("s3_invalid_observation")
    return {
        "kind": "s3",
        "bucket": bucket,
        "key": key,
        "size": size,
        "etag": etag,
        "last_modified": modified.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "version_id": version,
    }


def _s3_read_observation(response, bucket, key):
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
    ):
        raise ReferenceError("s3_invalid_response")
    return _s3_observation(response, bucket, key)


def _require_s3_conditions(client, params, etag):
    invalid = _INVALID_ETAG if etag != _INVALID_ETAG else '"aap-other-impossible-etag"'
    try:
        response = client.get_object(**{**params, "IfMatch": invalid})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "PreconditionFailed",
            "412",
        }:
            raise
    else:
        with _closing(response["Body"]):
            raise ReferenceError("s3_conditions_not_supported")


class S3Reader:
    def __init__(self, config, secret, allowlist):
        self.anchor = s3_anchor(config)
        self.bucket = self.anchor["bucket"]
        self.prefix = self.anchor["prefix"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise ReferenceError("s3_bucket_not_supported")
        self._config = {**config, "endpoint": self.anchor["endpoint"]}
        self._secret = secret
        self._allowlist = allowlist

    @contextmanager
    def _client(self):
        try:
            with _closing(
                open_pinned_s3(
                    self._config,
                    self._secret,
                    self._allowlist,
                    connect_timeout=2,
                    read_timeout=2,
                )
            ) as client:
                yield client
        except ConnectorHostDenied:
            raise ReferenceError("connector_host_denied") from None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                raise ReferenceError(
                    "source_changed", storage_code="PreconditionFailed"
                ) from None
            if code in {"AccessDenied", "403"}:
                raise ReferenceError(
                    "permission_denied", storage_code="AccessDenied"
                ) from None
            if code in {"InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise ReferenceError(
                    "content_source_unavailable", storage_code="AccessDenied"
                ) from None
            if code in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}:
                raise ReferenceError(
                    "path_unavailable", storage_code="NoSuchKey"
                ) from None
            if code in {
                "301",
                "302",
                "307",
                "308",
                "PermanentRedirect",
                "TemporaryRedirect",
                "AuthorizationHeaderMalformed",
            }:
                raise ReferenceError("s3_redirect_denied") from None
            raise ReferenceError("content_source_unavailable") from None
        except BotoCoreError:
            raise ReferenceError("content_source_unavailable") from None

    def list_page(self, relative, cursor=None, *, directories=False):
        prefix = s3_directory_prefix(self.prefix, relative)
        if len(prefix.encode()) > 1024:
            raise ReferenceError("invalid_s3_prefix")
        args = {
            "Bucket": self.bucket,
            "Prefix": prefix,
            "MaxKeys": PAGE_SIZE,
            "EncodingType": "url",
        }
        if cursor is not None:
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 4096:
                raise ReferenceError("s3_invalid_cursor")
            args["ContinuationToken"] = cursor
        if directories:
            args["Delimiter"] = "/"
        with self._client() as client:
            response = client.list_objects_v2(**args)
        if response.get("EncodingType") != "url":
            raise ReferenceError("s3_invalid_listing")
        next_cursor = (
            response.get("NextContinuationToken")
            if response.get("IsTruncated")
            else None
        )
        if response.get("IsTruncated") and (
            not isinstance(next_cursor, str)
            or not 0 < len(next_cursor) <= 4096
            or next_cursor == cursor
        ):
            raise ReferenceError("s3_invalid_cursor")
        objects, prefixes = (
            response.get("Contents", []),
            response.get("CommonPrefixes", []),
        )
        if len(objects) + len(prefixes) > PAGE_SIZE:
            raise ReferenceError("s3_invalid_listing")
        entries = []
        for obj in objects:
            key = _s3_key(obj.get("Key"))
            if not key.startswith(prefix):
                raise ReferenceError("s3_prefix_mismatch")
            if key.endswith("/"):
                continue
            observed = _s3_observation(
                {**obj, "ContentLength": obj.get("Size")}, self.bucket, key
            )
            observed.pop("version_id")
            entries.append({"name": key[len(prefix) :], **observed})
        names = []
        for obj in prefixes:
            key = _s3_key(obj.get("Prefix"))
            if (
                not key.startswith(prefix)
                or not key.endswith("/")
                or len(key) <= len(prefix)
            ):
                raise ReferenceError("s3_prefix_mismatch")
            names.append(key[len(prefix) :])
        return {
            "entries": entries,
            "directories": names,
            "next_cursor": next_cursor,
            "listed_count": len(objects) + len(prefixes),
        }

    @contextmanager
    def content_file(self, base, path, *, expected=None):
        key = s3_directory_prefix(self.prefix, base) + path
        if "\x00" in key or len(key.encode()) > 1024:
            raise ReferenceError("s3_invalid_key")
        if not path or (
            expected
            and (expected.get("key"), expected.get("bucket")) != (key, self.bucket)
        ):
            raise ReferenceError("content_binding_changed")
        args = {"Bucket": self.bucket, "Key": key}
        if expected and expected.get("version_id") not in {None, "null"}:
            args["VersionId"] = expected["version_id"]
        if expected and expected.get("etag"):
            args["IfMatch"] = expected["etag"]
        with self._client() as client:
            before = _s3_read_observation(client.head_object(**args), self.bucket, key)
            if expected and any(
                before.get(field) != value for field, value in expected.items()
            ):
                raise ReferenceError("source_changed")
            args["IfMatch"] = before["etag"]
            if before["version_id"] not in {None, "null"}:
                args["VersionId"] = before["version_id"]
            _require_s3_conditions(client, args, before["etag"])
            response = client.get_object(**args)
            with _closing(response["Body"]) as body:
                if _s3_read_observation(response, self.bucket, key) != before:
                    raise ReferenceError("source_changed")
                yield body, before
            if (
                _s3_read_observation(client.head_object(**args), self.bucket, key)
                != before
            ):
                raise ReferenceError("source_changed")


def _sftp_observation(info, path):
    result = {
        name: getattr(info, "st_" + name, None)
        for name in ("mode", "size", "mtime", "uid", "gid")
    }
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise ReferenceError("sftp_invalid_attributes")
    return {"protocol": "sftp", "path": path, **result}


def _sftp_path(sftp, path, *, directory, deadline):
    parts = path.split("/") if path else []
    remote = "/"
    result = None
    for index in range(len(parts) + 1):
        if time.monotonic() > deadline:
            raise ReferenceError(
                "directory_budget_exceeded" if directory else "content_read_timeout",
                409 if directory else 504,
            )
        info = sftp.lstat(remote)
        result = _sftp_observation(info, remote)
        mode = result["mode"]
        if stat.S_ISLNK(mode):
            raise ReferenceError("symlink_rejected")
        if index < len(parts) or directory:
            if not stat.S_ISDIR(mode):
                raise ReferenceError("not_a_directory")
        elif not stat.S_ISREG(mode):
            raise ReferenceError("not_a_regular_file")
        if index < len(parts):
            remote = remote.rstrip("/") + "/" + parts[index]
    return result


class SftpReader:
    def __init__(self, secret, allowlist, handle, root):
        self.anchor = sftp_anchor(handle, root)
        self._root = root
        self._secret = secret
        self._allowlist = allowlist

    @contextmanager
    def _client(self):
        host, port = self._root["host"], self._root["port"]
        try:
            address = resolve_allowed_ips(host, list(self._allowlist))[0]
            try:
                key = load_sftp_private_key(self._secret)
            except (ValueError, TypeError):
                raise ReferenceError("connection_unavailable") from None
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            with _closing(socket.socket(family, socket.SOCK_STREAM)) as connection:
                with _closing(paramiko.SSHClient()) as client:
                    entry = paramiko.hostkeys.HostKeyEntry.from_line(
                        "qualified " + self._root["host_key"]
                    )
                    server = host if port == 22 else f"[{host}]:{port}"
                    client.get_host_keys().add(server, entry.key.get_name(), entry.key)
                    client.set_missing_host_key_policy(paramiko.RejectPolicy())
                    connection.settimeout(2)
                    connection.connect((address, port))
                    client.connect(
                        hostname=host,
                        port=port,
                        username=self._root["username"],
                        password=self._secret.get("password") if key is None else None,
                        pkey=key,
                        allow_agent=False,
                        look_for_keys=False,
                        timeout=2,
                        banner_timeout=2,
                        auth_timeout=2,
                        channel_timeout=2,
                        sock=connection,
                    )
                    with _closing(client.open_sftp()) as sftp:
                        sftp.get_channel().settimeout(2)
                        yield sftp
        except ConnectorHostDenied:
            raise ReferenceError("connector_host_denied") from None
        except paramiko.BadHostKeyException:
            raise ReferenceError("sftp_host_key_mismatch") from None
        except paramiko.AuthenticationException:
            raise ReferenceError(
                "connection_unavailable", storage_code="AccessDenied"
            ) from None
        except (paramiko.SSHException, EOFError):
            raise ReferenceError("content_source_unavailable") from None
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise ReferenceError("path_unavailable") from None
            if exc.errno in {errno.EACCES, errno.EPERM}:
                raise ReferenceError(
                    "permission_denied", storage_code="AccessDenied"
                ) from None
            raise ReferenceError("content_source_unavailable") from None

    def scan_directory(self, path):
        path = sftp_relative_path(path)
        with self._client() as sftp:
            deadline = time.monotonic() + 2
            before = _sftp_path(sftp, path, directory=True, deadline=deadline)
            entries, names = [], set()
            with _closing(sftp.listdir_iter("/" + path, read_aheads=1)) as iterator:
                for info in iterator:
                    if len(entries) >= MAX_DIRECTORY_ENTRIES:
                        raise ReferenceError("directory_too_large")
                    if time.monotonic() > deadline:
                        raise ReferenceError("directory_budget_exceeded")
                    name = sftp_relative_path(info.filename)
                    if not name or "/" in name or name in names:
                        raise ReferenceError("sftp_invalid_listing")
                    names.add(name)
                    remote = "/" + (path + "/" if path else "") + name
                    observed = _sftp_observation(info, remote)
                    mode = observed["mode"]
                    kind = (
                        "directory"
                        if stat.S_ISDIR(mode)
                        else "file"
                        if stat.S_ISREG(mode)
                        else "symlink"
                        if stat.S_ISLNK(mode)
                        else "special"
                    )
                    entries.append({"name": name, "kind": kind, **observed})
            if _sftp_path(sftp, path, directory=True, deadline=deadline) != before:
                raise ReferenceError("directory_changed")
            entries.sort(key=lambda entry: entry["name"])
            digest = hashlib.sha256(
                json.dumps([before, entries], sort_keys=True).encode()
            ).hexdigest()
            if time.monotonic() > deadline:
                raise ReferenceError("directory_budget_exceeded")
            return {"identity": ["sftp", path], "digest": digest, "entries": entries}

    def browse_directory(self, path, limit, after, snapshot):
        manifest = self.scan_directory(path)
        if snapshot is not None and snapshot != manifest["digest"]:
            raise ReferenceError("directory_changed")
        names = [
            entry["name"]
            for entry in manifest["entries"]
            if entry["kind"] == "directory" and (after is None or entry["name"] > after)
        ]
        return {
            "names": names[:limit],
            "has_more": len(names) > limit,
            "snapshot": manifest["digest"],
        }

    @contextmanager
    def content_file(self, base, path, *, expected=None):
        base, path = sftp_relative_path(base), sftp_relative_path(path)
        if not path:
            raise ReferenceError("invalid_relative_path")
        relative = sftp_relative_path(base + "/" + path if base else path)
        with self._client() as sftp:
            deadline = time.monotonic() + 10
            before = _sftp_path(sftp, relative, directory=False, deadline=deadline)
            if expected and any(
                before.get(k) != v for k, v in expected.items() if k != "kind"
            ):
                raise ReferenceError("source_changed")
            with _closing(sftp.open("/" + relative, "rb", bufsize=0)) as body:
                body.settimeout(2)
                if _sftp_observation(body.stat(), "/" + relative) != before:
                    raise ReferenceError("source_changed")
                yield body, before
                if _sftp_observation(body.stat(), "/" + relative) != before:
                    raise ReferenceError("source_changed")
            if _sftp_path(sftp, relative, directory=False, deadline=deadline) != before:
                raise ReferenceError("source_changed")


def probe(reader, base, sample, expected):
    s3 = isinstance(reader, S3Reader)
    capabilities = {
        name: "unsupported" for name in ("range", "write", "delete", "presign")
    }
    capabilities.update(
        list="unknown",
        read="unknown",
        conditional_read="unknown" if s3 else "unsupported",
    )
    try:
        if s3:
            reader.list_page("")
        else:
            reader.scan_directory("")
        capabilities["list"] = "verified"
        if sample is not None:
            with reader.content_file(base, sample, expected=expected) as (body, _):
                body.read(16)
            capabilities["read"] = "verified"
            if s3:
                capabilities["conditional_read"] = "verified"
        return {
            "ok": True,
            "code": "ok",
            "capabilities": capabilities,
            "anchor": reader.anchor,
        }
    except ReferenceError as exc:
        if exc.unsettled:
            raise
        return {
            "ok": False,
            "code": exc.code,
            "capabilities": capabilities,
            "anchor": reader.anchor,
        }
