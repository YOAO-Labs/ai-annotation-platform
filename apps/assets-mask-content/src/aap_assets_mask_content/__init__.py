"""Portable content-addressed COCO RLE Mask object contract."""

from .contract import (
    CONTENT_SCHEMA,
    Candidate,
    CandidatePage,
    DeleteRequest,
    DeleteResult,
    ErrorCode,
    GetRequest,
    GetResult,
    ListRequest,
    MaskContentFailure,
    MaskReference,
    PutRequest,
    PutResult,
    RleContent,
)

__all__ = [
    "CONTENT_SCHEMA",
    "Candidate",
    "CandidatePage",
    "DeleteRequest",
    "DeleteResult",
    "ErrorCode",
    "GetRequest",
    "GetResult",
    "ListRequest",
    "MaskContentFailure",
    "MaskReference",
    "PutRequest",
    "PutResult",
    "RleContent",
]
