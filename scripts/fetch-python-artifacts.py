#!/usr/bin/env python3
"""Stage Python wheels from an explicitly identified, hash-verified manifest."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


HEX = re.compile(r"[0-9a-f]{64}\Z")


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def checked(data, expected, label):
    if not isinstance(expected, str) or not HEX.fullmatch(expected):
        raise ValueError(f"invalid SHA256 for {label}")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"artifact hash mismatch: {label}")
    return data


def fetch(manifest, sha256, output, packages=()):
    parsed = urlsplit(manifest)
    if parsed.scheme in ("http", "https", "file"):

        def read_artifact(name=None):
            url = urljoin(manifest, name) if name else manifest
            with urlopen(url, timeout=120) as response:
                return response.read()

    elif not parsed.scheme:
        source = Path(manifest)

        def read_artifact(name=None):
            return (source.parent / name if name else source).read_bytes()

    else:
        raise ValueError(f"unsupported manifest URL scheme: {parsed.scheme}")

    document = json.loads(checked(read_artifact(), sha256, "manifest"))
    wanted = {canonical(name) for name in packages}
    selected = {}
    for item in document["files"]:
        name = item["file"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or "\\" in name
            or name in ("", ".", "..")
        ):
            raise ValueError("invalid artifact filename")
        if not name.endswith(".whl"):
            continue
        if len(name[:-4].split("-")) < 5:
            raise ValueError(f"invalid wheel filename: {name}")
        distribution = canonical(name.split("-", 1)[0])
        if wanted and distribution not in wanted:
            continue
        if distribution in selected:
            raise ValueError(f"multiple wheels for {distribution}")
        selected[distribution] = (name, item["sha256"])
    missing = wanted - selected.keys()
    if missing:
        raise ValueError(f"requested wheels absent from manifest: {sorted(missing)}")
    if not selected:
        raise ValueError("manifest contains no selected wheels")
    verified = [
        (name, checked(read_artifact(name), digest, name))
        for name, digest in selected.values()
    ]
    output.mkdir(parents=True, exist_ok=True)
    for name, data in verified:
        (output / name).write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        required=True,
        help="manifest path or URL; wheels must be alongside it",
    )
    parser.add_argument("--sha256", required=True, help="SHA256 of the manifest bytes")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="distribution to fetch; repeat to select several",
    )
    args = parser.parse_args()
    fetch(args.manifest, args.sha256, args.output, args.package)
