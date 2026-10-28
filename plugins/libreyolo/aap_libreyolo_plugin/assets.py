"""Prepare only digest-pinned assets from the packaged canonical manifest."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


def assets():
    manifest = json.loads(files(__package__).joinpath("controlled-model-assets.json").read_text())
    return {item["base_model"][-1]: item for item in manifest["assets"]
            if item["framework"] == "libreyolo" and item["base_model"] in
            {"libreyolo9t", "libreyolo9s", "libreyolo9m", "libreyolo9c"}}


def verify(path, item):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size_bytes"]:
        raise ValueError("Pretrained asset missing or wrong size")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != item["sha256"]:
        raise ValueError("Pretrained asset digest differs")
    return path


def prepare(size, directory):
    item = assets()[size]
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / item["filename"]
    if destination.exists():
        return verify(destination, item)
    url = item["source_url"]
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        temporary = Path(output.name)
        try:
            with httpx.Client(timeout=120, follow_redirects=False) as client:
                for _ in range(5):
                    parsed = urlsplit(url)
                    host = (parsed.hostname or "").lower()
                    if (parsed.scheme != "https" or parsed.username or parsed.password
                        or not (host == "huggingface.co" or host.endswith(".huggingface.co")
                                or host == "hf.co" or host.endswith(".hf.co"))):
                        raise ValueError("Unapproved pretrained asset redirect")
                    with client.stream("GET", url) as response:
                        if response.is_redirect:
                            url = urljoin(url, response.headers["location"])
                            continue
                        response.raise_for_status()
                        total = 0
                        for chunk in response.iter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > item["size_bytes"]:
                                raise ValueError("Pretrained asset exceeds pinned size")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                        break
                else:
                    raise ValueError("Too many asset redirects")
            verify(temporary, item)
            os.chmod(temporary, 0o400)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
