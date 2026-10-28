"""Bounded image inputs: no filesystem access or unapproved URL hosts."""
from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def fetch_image(source: str, allowed_hosts: Sequence[str]) -> Image.Image:
    if source.startswith("data:"):
        header, sep, body = source.partition(",")
        if not sep or not header.startswith("data:image/") or not header.endswith(";base64") or len(body) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("invalid or oversized image data URL")
        try:
            data = base64.b64decode(body, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid image base64") from exc
    else:
        url = urlsplit(source)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment
                or url.hostname.lower() not in {host.lower() for host in allowed_hosts}):
            raise ValueError("image URL must use an explicitly allowed HTTP host")
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", source) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("image exceeds byte limit")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds byte limit")
    with Image.open(io.BytesIO(data)) as image:
        if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("image exceeds pixel limit")
        return image.convert("RGB")
