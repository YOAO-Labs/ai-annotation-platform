"""Standalone standard-library fetchers for receipt-bound LiDAR export media."""

_FETCH_TEMPLATE = r'''#!/usr/bin/env python3
"""Fetch exact exported media, verifying original SHA-256 and byte receipts."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = "@MANIFEST@"
ITEMS = "@ITEMS@"
DIRECTORY = "@DIRECTORY@"
SIZE_FIELD = "@SIZE_FIELD@"
SHA_FIELD = "@SHA_FIELD@"
MAX_BYTES = 256 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
_DIRFD = (
    hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
    and all(call in os.supports_dir_fd for call in (os.open, os.mkdir, os.stat, os.unlink, os.rename))
)


class Invalid(ValueError):
    pass


class Expired(Invalid):
    pass


class MissingBaseOrigin(Invalid):
    pass


class InvalidBaseOrigin(Invalid):
    pass


def safe_url_text(value):
    return (
        isinstance(value, str) and bool(value) and value.isascii()
        and "\\" not in value
        and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
    )


def http_authority(parts):
    try:
        return (
            parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and "%" not in parts.netloc and not parts.netloc.endswith(":")
            and (parts.port is None or parts.port > 0)
        )
    except ValueError:
        return False


def base_origin(value):
    if value is None:
        return None
    try:
        if not safe_url_text(value):
            raise ValueError
        parts = urlsplit(value)
        if not http_authority(parts) or parts.path or "?" in value or "#" in value:
            raise ValueError
    except ValueError:
        raise InvalidBaseOrigin("invalid_base_origin") from None
    return value


def media_url(value, origin):
    if not safe_url_text(value) or "#" in value:
        raise Invalid("invalid_media_url")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise Invalid("invalid_media_url") from None
    if value.startswith("/"):
        if value.startswith("//") or parts.scheme or parts.netloc:
            raise Invalid("invalid_media_url")
        if origin is None:
            raise MissingBaseOrigin("relative_media_url_requires_base_origin")
        # Concatenation preserves the signed path/query exactly; urljoin can
        # normalize segments or replace an origin for protocol-relative input.
        return origin + value
    if not http_authority(parts):
        raise Invalid("invalid_media_url")
    return value


def deadline(value):
    if not isinstance(value, str):
        raise Invalid("invalid_expiry")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Invalid("invalid_expiry") from None
    if result.utcoffset() is None:
        raise Invalid("invalid_expiry")
    return result.astimezone(timezone.utc)


def check_deadline(expires):
    if datetime.now(timezone.utc) >= expires:
        raise Expired("expired_media_manifest")


def safe_destination(relative):
    if (
        not isinstance(relative, str) or not relative or "\\" in relative
        or ":" in relative or "\0" in relative
    ):
        raise Invalid("unsafe_media_path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Invalid("unsafe_media_path")
    root = os.path.realpath(HERE)
    path = root
    for part in (DIRECTORY, *parts):
        path = os.path.join(path, part)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise Invalid("unsafe_media_path")
    if os.path.commonpath((root, os.path.realpath(path))) != root:
        raise Invalid("unsafe_media_path")
    return path


@contextmanager
def destination_parent(relative):
    path = safe_destination(relative)
    parent = None
    try:
        if _DIRFD:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(os.path.realpath(HERE), flags)
            for part in (DIRECTORY, *relative.split("/")[:-1]):
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=parent)
                os.close(parent)
                parent = child
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            safe_destination(relative)
        yield parent, path
    finally:
        if parent is not None:
            os.close(parent)


def open_in_parent(parent, path, flags, mode=0o600):
    if parent is not None:
        return os.open(os.path.basename(path), flags, mode, dir_fd=parent)
    return os.open(path, flags, mode)


@contextmanager
def owned_file(descriptor, mode):
    try:
        stream = os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def file_matches(parent, path, expected_size, expected_sha256):
    try:
        descriptor = open_in_parent(
            parent, path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        return False
    with owned_file(descriptor, "rb") as source:
        facts = os.fstat(source.fileno())
        if not stat.S_ISREG(facts.st_mode):
            raise Invalid("unsafe_media_destination")
        if facts.st_size != expected_size:
            return False
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(CHUNK_BYTES):
            size += len(chunk)
            if size > expected_size:
                return False
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if (facts.st_size, facts.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return False
    return size == expected_size and digest.hexdigest() == expected_sha256


def validated_items(manifest, base_url=None):
    if not isinstance(manifest, dict) or not isinstance(manifest.get(ITEMS), list):
        raise Invalid("invalid_media_manifest")
    expires = deadline(manifest.get("expires_at"))
    check_deadline(expires)
    origin = base_origin(base_url)
    result, seen = [], set()
    for item in manifest[ITEMS]:
        if not isinstance(item, dict):
            raise Invalid("invalid_media_receipt")
        size, sha256 = item.get(SIZE_FIELD), item.get(SHA_FIELD)
        if type(size) is not int or not 0 <= size <= MAX_BYTES:
            raise Invalid("invalid_media_size")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise Invalid("invalid_media_sha256")
        relative, url = item.get("rel_path"), item.get("presigned_url")
        destination = os.path.normcase(safe_destination(relative))
        if destination in seen:
            raise Invalid("duplicate_media_path")
        seen.add(destination)
        url = media_url(url, origin)
        item_expires = min(expires, deadline(item["expires_at"])) if "expires_at" in item else expires
        check_deadline(item_expires)
        result.append((relative, url, size, sha256.lower(), item_expires))
    return result


def download_response(url):
    try:
        return urllib.request.urlopen(url, timeout=30)
    except urllib.error.HTTPError as response:
        response.close()
        raise Invalid("unexpected_http_status") from None


def fetch(item):
    relative, url, expected_size, expected_sha256, expires = item
    check_deadline(expires)
    with destination_parent(relative) as (parent, destination):
        if file_matches(parent, destination, expected_size, expected_sha256):
            check_deadline(expires)
            return
        temporary = None
        try:
            candidate = os.path.join(os.path.dirname(destination), ".aap-download-" + uuid.uuid4().hex)
            descriptor = open_in_parent(
                parent, candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            )
            temporary = candidate
            with owned_file(descriptor, "wb") as output:
                check_deadline(expires)
                with download_response(url) as response:
                    if response.status != 200:
                        raise Invalid("unexpected_http_status")
                    lengths = response.headers.get_all("Content-Length", [])
                    if lengths and (
                        len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None
                        or int(lengths[0]) != expected_size
                    ):
                        raise Invalid("download_length_mismatch")
                    digest, size = hashlib.sha256(), 0
                    while chunk := response.read(CHUNK_BYTES):
                        check_deadline(expires)
                        size += len(chunk)
                        if size > expected_size:
                            raise Invalid("download_length_mismatch")
                        digest.update(chunk)
                        output.write(chunk)
                    if size != expected_size or digest.hexdigest() != expected_sha256:
                        raise Invalid("download_receipt_mismatch")
            check_deadline(expires)
            safe_destination(relative)
            if parent is not None:
                os.replace(os.path.basename(temporary), os.path.basename(destination), src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    if parent is not None:
                        os.unlink(os.path.basename(temporary), dir_fd=parent)
                    else:
                        os.unlink(temporary)
                except FileNotFoundError:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Platform HTTP(S) origin for root-relative signed URLs")
    arguments = parser.parse_args([] if argv is None else argv)
    try:
        with open(os.path.join(HERE, MANIFEST), encoding="utf-8") as source:
            items = validated_items(json.load(source), base_url=arguments.base_url)
    except MissingBaseOrigin:
        print("[x] Relative media URL requires --base-url https://your-platform (origin only).")
        return 1
    except InvalidBaseOrigin:
        print("[x] Invalid --base-url: use an HTTP(S) origin without credentials, path, query or fragment.")
        return 1
    except Expired:
        print("[!] Media links expired; export again.")
        return 2
    except Exception as exc:
        print("[x] Invalid manifest: %s" % type(exc).__name__)
        return 1
    ok, fail, expired = 0, 0, False
    for item in items:
        try:
            fetch(item)
            ok += 1
        except Expired:
            expired, fail = True, fail + 1
            print("[!] Media links expired; export again.")
        except Exception as exc:
            fail += 1
            # HTTP and OS exceptions can contain signed URLs. Never render them.
            print("[x] Download failed: %s" % type(exc).__name__)
    print("[done] success %d, failed %d; %s/" % (ok, fail, DIRECTORY))
    return 2 if expired else (1 if fail else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _template(
    manifest: str,
    items: str,
    directory: str,
    *,
    size_field: str = "size_bytes",
    sha_field: str = "sha256",
) -> str:
    return (
        _FETCH_TEMPLATE.replace("@MANIFEST@", manifest)
        .replace("@ITEMS@", items)
        .replace("@DIRECTORY@", directory)
        .replace("@SIZE_FIELD@", size_field)
        .replace("@SHA_FIELD@", sha_field)
    )


FETCH_POINTCLOUDS_TEMPLATE = _template(
    "pointclouds_manifest.json", "pointclouds", "velodyne"
)
FETCH_LIDAR_IMAGES_TEMPLATE = _template("images_manifest.json", "images", "images")
FETCH_TRUSTED_MEDIA_TEMPLATE = _template(
    "media_manifest.json",
    "media",
    ".",
    size_field="source_file_size",
    sha_field="source_sha256",
)
