"""Opaque references are never object-store locators."""

import re
import uuid

PREFIX = "__aap_reference__/"
_PATTERN = re.compile(r"^__aap_reference__/([0-9a-f-]{36})/([0-9a-f]{64})$")


def is_reference(key):
    return isinstance(key, str) and key.startswith(PREFIX)


def reference_key(item_id, sha256):
    return f"{PREFIX}{item_id}/{sha256}"


def parse_reference(key):
    match = _PATTERN.fullmatch(key or "")
    if match is None:
        raise ValueError("Invalid reference identity")
    return uuid.UUID(match[1]), match[2]


def reject_reference(key):
    if is_reference(key):
        raise ValueError("Reference content requires authenticated verified access")
