"""Portable durable batch-result contract and streaming snapshot codec."""

from .contract import BatchResultFailure, SCHEMA
from .codec import (
    PayloadAssembler,
    Snapshot,
    bundle_digest,
    decode_part,
    encode_part,
    snapshot_file,
)

__all__ = [
    "BatchResultFailure",
    "PayloadAssembler",
    "SCHEMA",
    "Snapshot",
    "bundle_digest",
    "decode_part",
    "encode_part",
    "snapshot_file",
]
