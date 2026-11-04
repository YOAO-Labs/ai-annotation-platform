"""Pure validators for stable physical GPU/MIG resource identities."""

from __future__ import annotations

import re


_GPU_UUID_RE = re.compile(r"^GPU-[A-Za-z0-9][A-Za-z0-9-]*$")
_MIG_UUID_RE = re.compile(
    r"^MIG-(?:GPU-)?[A-Za-z0-9][A-Za-z0-9._:-]*(?:/[0-9]+/[0-9]+)?$"
)
_INDEX_TOKEN_RE = re.compile(r"^index:(?:0|[1-9][0-9]*)$")


def validate_physical_device_token(value: str) -> str:
    """Accept GPU UUID, MIG UUID, or an explicit non-negative physical index."""

    if not (
        _GPU_UUID_RE.fullmatch(value)
        or _MIG_UUID_RE.fullmatch(value)
        or _INDEX_TOKEN_RE.fullmatch(value)
    ):
        raise ValueError(
            "physical_device_token 必须是 GPU UUID、MIG UUID 或 index:<非负整数>"
        )
    return value


def validate_gpu_resource_id(value: str) -> str:
    """Validate ``<resource_domain>/<physical_device_token>`` without guessing."""

    if value != value.strip() or any(ch.isspace() for ch in value):
        raise ValueError("gpu_resource_id 不得包含空白")
    if "," in value:
        raise ValueError("gpu_resource_id 只能引用单一资源，不得包含逗号")
    domain, separator, token = value.partition("/")
    if not separator or not domain or not token:
        raise ValueError(
            "gpu_resource_id 必须为 <resource_domain>/<physical_device_token>"
        )
    if "/" in domain:
        raise ValueError("resource_domain 不得包含 /")
    validate_physical_device_token(token)
    return value
