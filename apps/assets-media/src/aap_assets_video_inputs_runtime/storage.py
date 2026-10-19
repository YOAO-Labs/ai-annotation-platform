"""Reuse exact-version binary transfer with a separate video attempt allowlist."""

import re

from aap_assets_inputs.contract import InputFailure
from aap_assets_inputs_runtime.storage import S3InputStorage
from aap_assets_video_inputs import values as c


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
