"""Compose receipt-bound image downloads from the hardened LiDAR primitives."""

from app.services.exporting.lidar_fetch import _template


_BASE = _template("images_manifest.json", "images", ".")
_PRIMITIVES = _BASE[: _BASE.index("def validated_items(")].replace(
    'or ":" in relative or "\\0" in relative',
    'or ":" in relative or "\\0" in relative or any(ord(char) < 32 or ord(char) == 127 for char in relative)',
)
_DOWNLOAD = _BASE[
    _BASE.index("def download_response(") : _BASE.index("def main(")
].replace("def fetch(item):", "def download(item):")

FETCH_IMAGES_TEMPLATE = (
    _PRIMITIVES
    + _DOWNLOAD
    + r"""
def validated_items(manifest, base_url=None):
    if not isinstance(manifest, dict) or not isinstance(manifest.get(ITEMS), list):
        raise Invalid("invalid_media_manifest")
    if len(manifest[ITEMS]) > 20000:
        raise Invalid("invalid_media_manifest")
    expires = deadline(manifest.get("expires_at"))
    origin = base_origin(base_url)
    result, seen = [], set()
    for item in manifest[ITEMS]:
        if not isinstance(item, dict):
            raise Invalid("invalid_media_receipt")
        size, sha256 = item.get(SIZE_FIELD), item.get(SHA_FIELD)
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise Invalid("invalid_media_size")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise Invalid("invalid_media_sha256")
        embedded = item.get("storage_mode") == "embedded"
        if item.get("storage_mode") not in (None, "managed", "embedded"):
            raise Invalid("invalid_media_receipt")
        paths = item.get("archive_paths")
        if paths is None:
            paths = [item.get("archive_path") or "images/" + str(item.get("rel_path") or "")]
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            raise Invalid("invalid_media_paths")
        destinations = []
        for relative in paths:
            destination = os.path.normcase(safe_destination(relative))
            if destination in seen:
                raise Invalid("duplicate_media_path")
            seen.add(destination)
            destinations.append(relative)
        item_expires = min(expires, deadline(item["expires_at"])) if "expires_at" in item else expires
        # Embedded bytes have no expiring network dependency. Do not resolve a
        # signed URL or require a platform origin for their local verification.
        expired = datetime.now(timezone.utc) >= item_expires
        url = None if embedded else media_url(item.get("presigned_url"), origin or ("https://expired.invalid" if expired else None))
        result.append((tuple(destinations), url, size, sha256.lower(), item_expires))
    return result


def fetch(item):
    paths, url, size, sha256, expires = item
    if url is None:
        for relative in paths:
            with destination_parent(relative) as (parent, destination):
                if not file_matches(parent, destination, size, sha256):
                    raise Invalid("embedded_content_mismatch")
        return
    for relative in paths:
        download((relative, url, size, sha256, expires))


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
    except Exception as exc:
        print("[x] Invalid manifest: %s" % type(exc).__name__)
        return 1
    ok, fail, expired = 0, 0, False
    for item in items:
        try:
            fetch(item)
            ok += 1
            if item[1] is None:
                print("[ok] embedded SHA-256")
        except Expired:
            expired, fail = True, fail + 1
            print("[!] Media links expired; export again.")
        except Exception as exc:
            fail += 1
            label = "Embedded verification failed" if item[1] is None else "Download failed"
            print("[x] %s: %s" % (label, type(exc).__name__))
    print("[done] success %d, failed %d; images/" % (ok, fail))
    return 2 if expired else (1 if fail else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""
)
