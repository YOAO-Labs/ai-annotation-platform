"""Reuse exact-version binary transfer with a separate video attempt allowlist."""

from dataclasses import dataclass
from pathlib import Path
import re

from aap_assets_images.contract import ObjectIdentity as TransportIdentity

from aap_assets_inputs.contract import InputFailure
from aap_assets_inputs_runtime.storage import S3InputStorage
from aap_assets_video_inputs import values as c
from aap_assets_video_inputs.execution import VideoCaptureSource


@dataclass(frozen=True)
class _SourceObject:
    """Byte-transfer facts; this is not an image domain source or owner grant."""

    file_path: str
    file_size: int | None
    content_hash: str | None
    object_identity: TransportIdentity | None
    bucket_kind: str = "datasets"


@dataclass(frozen=True)
class VideoDownload:
    identity: c.ObjectIdentity
    byte_size: int
    sha256: str


class VideoSourceStorage:
    """Reuse conditional binary source transfer without fabricating ImageSource."""

    def __init__(self, binary_storage):
        self.binary = binary_storage

    @staticmethod
    def _source(source: VideoCaptureSource) -> _SourceObject:
        return _SourceObject(
            file_path=source.file_path,
            file_size=source.file_size,
            content_hash=source.content_hash,
            object_identity=TransportIdentity(**source.object_identity.model_dump())
            if source.object_identity is not None
            else None,
        )

    async def head_source(self, source: VideoCaptureSource) -> c.ObjectIdentity:
        result = await self.binary.head_source(self._source(source))
        return c.ObjectIdentity(**result.model_dump())

    async def download_source(
        self, source: VideoCaptureSource, destination: Path
    ) -> VideoDownload:
        result = await self.binary.download_source(self._source(source), destination)
        return VideoDownload(
            c.ObjectIdentity(**result.identity.model_dump()),
            result.byte_size,
            result.sha256,
        )


class S3VideoInputStorage(S3InputStorage):
    @staticmethod
    def _key_type(key):
        if isinstance(key, str) and len(key) <= 2048:
            for model, pattern in (
                (c.VideoBlob, c.VIDEO_BLOB_KEY_PATTERN),
                (c.FrameBlob, c.FRAME_BLOB_KEY_PATTERN),
            ):
                if re.fullmatch(pattern, key) is not None:
                    return model
        raise InputFailure("invalid_request")

    @staticmethod
    def _check_key(key):
        S3VideoInputStorage._key_type(key)

    @staticmethod
    def _check_prefix(prefix):
        if (
            not isinstance(prefix, str)
            or re.fullmatch(c.ATTEMPT_PREFIX_PATTERN, prefix) is None
        ):
            raise InputFailure("invalid_request")

    def _blob(self, **values):
        return self._key_type(values["key"])(**values)
