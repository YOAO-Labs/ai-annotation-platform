from __future__ import annotations

import struct
from typing import BinaryIO

from aap_assets_source.archive import ArchiveSafetyError

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def inspect_png_header(source: BinaryIO) -> tuple[int, int, int, int]:
    header = source.read(33)
    if len(header) < 33 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ArchiveSafetyError(
            "png_magic_invalid", "PNG signature or IHDR is invalid"
        )
    width, height, bit_depth, color_type = struct.unpack(">IIBB", header[16:26])
    if width <= 0 or height <= 0:
        raise ArchiveSafetyError(
            "png_dimensions_invalid", "PNG dimensions must be positive"
        )
    return width, height, bit_depth, color_type


def validate_png_contract(
    source: BinaryIO,
    *,
    expected_width: int,
    expected_height: int,
    allowed_bit_depths: frozenset[int] = frozenset({8}),
    allowed_color_types: frozenset[int] = frozenset({0, 3}),
) -> None:
    width, height, bit_depth, color_type = inspect_png_header(source)
    if (width, height) != (expected_width, expected_height):
        raise ArchiveSafetyError(
            "image_size_mismatch",
            "PNG dimensions do not match the manifest",
            detail={
                "expected": [expected_width, expected_height],
                "observed": [width, height],
            },
        )
    if bit_depth not in allowed_bit_depths or color_type not in allowed_color_types:
        raise ArchiveSafetyError(
            "png_contract_unsupported",
            "PNG bit depth or color type is unsupported",
            detail={"bit_depth": bit_depth, "color_type": color_type},
        )
