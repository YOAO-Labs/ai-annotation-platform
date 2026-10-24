"""Pure original-record and bounded pointmask contracts; no database fixtures."""

import io
import math
import struct
import tracemalloc
from types import SimpleNamespace

import pytest

from app.services.exporting import lidar
from app.services.exporting import lidar_pcd as pcd


FIELDS = (
    ("intensity", 1, "U", 1),
    ("z", 4, "F", 1),
    ("x", 4, "F", 1),
    ("y", 4, "F", 1),
)
ROWS = ((7, 3, 1, 2), (8, 9, float("nan"), 4), (9, 5, 6, float("inf")))


def header(mode, count, *, fields=FIELDS, width=None, height=1):
    return (
        "# .PCD v0.7 - original rows, including non-finite XYZ\n"
        "VERSION .7\n"
        f"FIELDS {' '.join(field[0] for field in fields)}\n"
        f"SIZE {' '.join(str(field[1]) for field in fields)}\n"
        f"TYPE {' '.join(field[2] for field in fields)}\n"
        f"COUNT {' '.join(str(field[3]) for field in fields)}\n"
        f"WIDTH {count if width is None else width}\nHEIGHT {height}\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {count}\nDATA {mode}\n"
    ).encode()


def literal(data):
    return b"".join(
        bytes([len(data[start : start + 32]) - 1]) + data[start : start + 32]
        for start in range(0, len(data), 32)
    )


def backref(distance, length):
    assert 1 <= distance <= 8192 and 3 <= length <= 264
    high, low = divmod(distance - 1, 256)
    encoded_length = length - 2
    if encoded_length < 7:
        return bytes([(encoded_length << 5) | high, low])
    return bytes([(7 << 5) | high, encoded_length - 7, low])


def compressed_body(encoded, length):
    return struct.pack("<II", len(encoded), length) + encoded


def cloud(mode="ascii"):
    if mode == "ascii":
        body = b"\n" + b"".join(
            " ".join(str(value) for value in row).encode() + b"\n" for row in ROWS
        )
    elif mode == "binary":
        body = b"".join(struct.pack("<Bfff", *row) for row in ROWS)
    else:
        # PCD compressed bodies are field-major; they retain each field's point order.
        expanded = b"".join(
            struct.pack("<" + code * len(ROWS), *(row[index] for row in ROWS))
            for index, code in enumerate(("B", "f", "f", "f"))
        )
        body = compressed_body(literal(expanded), len(expanded))
    return header(mode, len(ROWS)) + body


def annotation(indices, *, class_name="car", source_count=3, **extra):
    return SimpleNamespace(
        class_name=class_name,
        geometry={
            "type": "point_mask_3d",
            "point_indices": indices,
            "source_point_count": source_count,
            **extra,
        },
    )


@pytest.mark.parametrize("mode", ["ascii", "binary", "binary_compressed"])
def test_three_formats_preserve_reordered_fields_nonfinite_rows_and_borrowed_cursor(
    mode,
):
    raw = cloud(mode)
    source = io.BytesIO(raw)
    source.seek(9)
    info = pcd.inspect_pcd(source)
    assert source.tell() == 9 and not source.closed
    assert (info.point_count, info.data_kind, info.point_stride, info.size_bytes) == (
        3,
        mode,
        13,
        len(raw),
    )
    assert info.body_offset == len(header(mode, 3))
    labels = lidar.build_pointmask_label_bytes(
        [annotation([1]), annotation([2], class_name="truck")],
        source_point_count=info.point_count,
        category_map={"car": 1, "truck": 2},
    )
    # NaN at original record 1 still occupies its label slot; record 2 never shifts.
    assert struct.unpack("<3I", labels) == (0, 1, 2)


def test_organized_ascii_optional_count_points_crlf_and_header_comments():
    raw = cloud().replace(b"COUNT 1 1 1 1\n", b"").replace(b"POINTS 3\n", b"")
    raw = raw.replace(b"WIDTH 3\nHEIGHT 1", b"WIDTH 1\nHEIGHT 3")
    raw = raw.replace(b"TYPE U F F F", b"TYPE U F F F # typed records")
    assert pcd.inspect_pcd(io.BytesIO(raw.replace(b"\n", b"\r\n"))).point_count == 3


@pytest.mark.parametrize("mode", ["ascii", "binary", "binary_compressed"])
def test_real_empty_cloud_has_zero_records_and_zero_labels(mode):
    body = compressed_body(b"", 0) if mode == "binary_compressed" else b""
    info = pcd.inspect_pcd(io.BytesIO(header(mode, 0) + body))
    assert info.point_count == 0
    assert (
        lidar.build_pointmask_label_bytes([], source_point_count=0, category_map={})
        == b""
    )


@pytest.mark.parametrize(
    "replacement,expected",
    [
        ((b"FIELDS intensity z x y", b"FIELDS intensity z x u"), "pcd_fields_invalid"),
        ((b"FIELDS intensity z x y", b"FIELDS intensity z x x"), "pcd_fields_invalid"),
        ((b"SIZE 1 4 4 4", b"SIZE 1 4 4"), "pcd_fields_invalid"),
        ((b"TYPE U F F F", b"TYPE U F F U"), "pcd_ascii_value_invalid"),
        ((b"SIZE 1 4 4 4", b"SIZE 3 4 4 4"), "pcd_fields_invalid"),
        ((b"COUNT 1 1 1 1", b"COUNT 0 1 1 1"), "pcd_fields_invalid"),
        ((b"COUNT 1 1 1 1", b"COUNT 1 2 1 1"), "pcd_fields_invalid"),
        ((b"WIDTH 3", b"WIDTH -3"), "pcd_header_invalid"),
        ((b"HEIGHT 1", b"HEIGHT 2"), "pcd_point_count_mismatch"),
        ((b"POINTS 3", b"POINTS 2"), "pcd_point_count_mismatch"),
        ((b"POINTS 3", b"POINTS 3\nPOINTS 3"), "pcd_header_invalid"),
        (
            (b"VIEWPOINT 0 0 0 1 0 0 0", b"VIEWPOINT 0 0 0 1 0 0 nan"),
            "pcd_header_invalid",
        ),
        ((b"DATA ascii", b"DATA unknown"), "pcd_data_mode_unsupported"),
        ((b"DATA ascii", b"UNKNOWN 1\nDATA ascii"), "pcd_header_invalid"),
        ((b"VERSION .7", b"VERSION 1.0"), "pcd_header_invalid"),
    ],
)
def test_ambiguous_or_invalid_header_never_returns_a_count_and_restores_cursor(
    replacement, expected
):
    source = io.BytesIO(cloud().replace(*replacement))
    source.seek(5)
    with pytest.raises(ValueError, match=expected):
        pcd.inspect_pcd(source)
    assert source.tell() == 5 and not source.closed


@pytest.mark.parametrize(
    "row",
    [
        b"7 3 1",
        b"7 3 1 2 5",
        b"256 3 1 2",
        b"-1 3 1 2",
        b"7 3 1 2garbage",
        b"7 3 1 1e999",
        b"7 3 1 3.5e38",
        b"7 3 1 true",
        b"7 3 1 0x10",
        b"7 3 1 \xff",
    ],
)
def test_ascii_checks_every_scalar_and_exact_row_shape(row):
    raw = header("ascii", 1) + row + b"\n"
    with pytest.raises(ValueError, match="pcd_ascii_"):
        pcd.inspect_pcd(io.BytesIO(raw))


@pytest.mark.parametrize("mode", ["ascii", "binary"])
@pytest.mark.parametrize("change", ["short", "extra"])
def test_data_body_is_exact_instead_of_truncating_to_declared_points(mode, change):
    raw = cloud(mode)
    if mode == "ascii":
        raw = (
            raw.rsplit(b"9 5 6 inf\n", 1)[0]
            if change == "short"
            else raw + b"1 2 3 4\n"
        )
    else:
        raw = raw[:-1] if change == "short" else raw + b"\x00"
    with pytest.raises(ValueError, match="pcd_(point_count|binary_length)_mismatch"):
        pcd.inspect_pcd(io.BytesIO(raw))


def test_ascii_vector_field_counts_are_validated_without_xyz_matrix():
    fields = (
        ("x", 4, "F", 1),
        ("histogram", 2, "I", 2),
        ("y", 4, "F", 1),
        ("z", 4, "F", 1),
    )
    raw = header("ascii", 1, fields=fields) + b"nan -32768 32767 inf -inf\n"
    info = pcd.inspect_pcd(io.BytesIO(raw))
    assert info.point_count == 1 and info.point_stride == 16
    with pytest.raises(ValueError, match="pcd_ascii_value_invalid"):
        pcd.inspect_pcd(io.BytesIO(raw.replace(b"32767", b"32768")))


@pytest.mark.parametrize(
    "setting,limit,expected",
    [
        ("MAX_HEADER_BYTES", 64, "pcd_header_budget_exceeded"),
        ("MAX_ROW_BYTES", 5, "pcd_row_budget_exceeded"),
        ("MAX_TOKEN_BYTES", 8, "pcd_token_budget_exceeded"),
        ("MAX_FIELDS", 3, "pcd_field_budget_exceeded"),
        ("MAX_FIELD_COUNT", 0, "pcd_field_budget_exceeded"),
        ("MAX_POINT_SCALARS", 3, "pcd_field_budget_exceeded"),
        ("MAX_POINT_STRIDE", 12, "pcd_field_budget_exceeded"),
        ("MAX_PCD_POINTS", 2, "pcd_point_budget_exceeded"),
    ],
)
def test_parser_rejects_each_independent_resource_budget(
    monkeypatch, setting, limit, expected
):
    monkeypatch.setattr(pcd, setting, limit)
    with pytest.raises(ValueError, match=expected):
        pcd.inspect_pcd(io.BytesIO(cloud()))


def test_actual_file_byte_limit_is_checked_before_any_header_read_and_is_inclusive(
    monkeypatch,
):
    raw = cloud()
    monkeypatch.setattr(pcd, "MAX_PCD_BYTES", len(raw))
    assert pcd.inspect_pcd(io.BytesIO(raw)).point_count == 3
    source = io.BytesIO(raw + b"\n")
    source.seek(4)
    with pytest.raises(ValueError, match="pcd_byte_budget_exceeded"):
        pcd.inspect_pcd(source)
    assert source.tell() == 4


def test_body_token_length_is_bounded_before_numeric_conversion():
    raw = header("ascii", 1) + b"7 3 1 " + b"1" * (pcd.MAX_TOKEN_BYTES + 1) + b"\n"
    with pytest.raises(ValueError, match="pcd_token_budget_exceeded"):
        pcd.inspect_pcd(io.BytesIO(raw))


def test_non_binary_or_non_seekable_stream_is_not_a_pcd_stage():
    source = io.StringIO(cloud().decode())
    source.seek(4)
    with pytest.raises(ValueError, match="pcd_stream_invalid"):
        pcd.inspect_pcd(source)
    assert source.tell() == 4

    class Pipe(io.BytesIO):
        def seekable(self):
            return False

    with pytest.raises(ValueError, match="pcd_stream_invalid"):
        pcd.inspect_pcd(Pipe(cloud()))


@pytest.mark.parametrize("interruption", [OSError, KeyboardInterrupt])
def test_borrowed_cursor_is_restored_after_actual_reader_baseexceptions(interruption):
    class Broken(io.BytesIO):
        def readline(self, size=-1):
            super().readline(size)
            raise interruption("reader interrupted")

    source = Broken(cloud())
    source.seek(11)
    with pytest.raises(interruption, match="reader interrupted"):
        pcd.inspect_pcd(source)
    assert source.tell() == 11 and not source.closed


def test_data_comments_are_not_silently_filtered_as_original_point_records():
    raw = cloud().replace(b"8 9 nan 4", b"# 8 9 nan 4")
    with pytest.raises(ValueError, match="pcd_ascii_row_invalid"):
        pcd.inspect_pcd(io.BytesIO(raw))


def test_compressed_parser_handles_short_reads_and_bounds_each_read():
    class ShortReads(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 64 * 1024
            return super().read(min(size, 3))

    source = ShortReads(cloud("binary_compressed"))
    source.seek(8)
    assert pcd.inspect_pcd(source).point_count == 3
    assert source.tell() == 8 and not source.closed


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x0b" + bytes(11),  # incomplete literal
        b"\x20\x00",  # back-reference before the start
        b"\xe0",  # missing extended length
        b"\xe0\x00",  # missing distance
        literal(b"\0"),  # incomplete output
        literal(bytes(13)),  # output exceeds the record body
        literal(bytes(12)) + b"\x00",  # unconsumed malformed trailing control
        literal(b"\0") + backref(2, 11),  # distance reaches before output start
    ],
)
def test_compressed_body_requires_valid_complete_lzf_input_and_output(encoded):
    fields = (("x", 4, "F", 1), ("y", 4, "F", 1), ("z", 4, "F", 1))
    source = io.BytesIO(
        header("binary_compressed", 1, fields=fields) + compressed_body(encoded, 12)
    )
    source.seek(7)
    with pytest.raises(ValueError, match="pcd_lzf_invalid"):
        pcd.inspect_pcd(source)
    assert source.tell() == 7 and not source.closed


@pytest.mark.parametrize(
    "change",
    [
        "header_short",
        "declared_compressed",
        "declared_expanded",
        "extra_body",
        "short_body",
    ],
)
def test_compressed_envelope_lengths_match_the_exact_record_layout(change):
    raw = cloud("binary_compressed")
    start = len(header("binary_compressed", 3))
    if change == "header_short":
        raw = raw[: start + 7]
    elif change == "declared_compressed":
        raw = raw[:start] + struct.pack("<I", 1) + raw[start + 4 :]
    elif change == "declared_expanded":
        raw = raw[: start + 4] + struct.pack("<I", 38) + raw[start + 8 :]
    elif change == "extra_body":
        raw += b"\0"
    else:
        raw = raw[:-1]
    with pytest.raises(ValueError, match="pcd_compressed_length_mismatch"):
        pcd.inspect_pcd(io.BytesIO(raw))


def test_compressed_expansion_has_an_independent_byte_budget(monkeypatch):
    monkeypatch.setattr(pcd, "MAX_DECOMPRESSED_BYTES", 38)
    with pytest.raises(ValueError, match="pcd_decompressed_budget_exceeded"):
        pcd.inspect_pcd(io.BytesIO(cloud("binary_compressed")))


@pytest.mark.parametrize(
    "prefix_length,distance,length", [(1, 1, 38), (8192, 8192, 256), (8210, 30, 264)]
)
def test_lzf_overlapping_and_ring_wrapped_backrefs_reconstruct_exact_original_bytes(
    monkeypatch, prefix_length, distance, length
):
    prefix = bytes(index % 251 for index in range(prefix_length))
    seed = prefix[-distance:]
    expected = prefix + (seed * math.ceil(length / distance))[:length]
    padding = bytes((-len(expected)) % 12)
    expected += padding
    encoded = literal(prefix) + backref(distance, length) + literal(padding)
    reconstructed = bytearray()
    append = pcd._append_history

    def observe(history, produced, data):
        assert len(history) == 8192 and len(data) <= 264
        reconstructed.extend(data)
        append(history, produced, data)

    monkeypatch.setattr(pcd, "_append_history", observe)
    fields = (("x", 4, "F", 1), ("y", 4, "F", 1), ("z", 4, "F", 1))
    raw = header(
        "binary_compressed", len(expected) // 12, fields=fields
    ) + compressed_body(encoded, len(expected))
    assert pcd.inspect_pcd(io.BytesIO(raw)).point_count == len(expected) // 12
    assert reconstructed == expected


def test_lzf_large_expansion_retains_constant_memory():
    steps = 20_000
    encoded = literal(b"\0") + backref(1, 264) * steps
    source = io.BytesIO(encoded)
    already_tracing = tracemalloc.is_tracing()
    if already_tracing:
        pytest.skip("a global tracer owns the memory measurements")
    tracemalloc.start()
    try:
        pcd._validate_lzf(source, len(encoded), 1 + 264 * steps)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024  # The expanded stream exceeds 5 MiB.


def test_pointmask_uses_exact_record_space_and_caller_order_without_stride_expansion():
    labels = lidar.build_pointmask_label_bytes(
        [
            annotation([0, 1], decimate_stride=100),
            annotation([1, 2], class_name="truck"),
            annotation([1]),
        ],
        source_point_count=3,
        category_map={"car": 1, "truck": 2},
    )
    assert struct.unpack("<3I", labels) == (1, 1, 2)
    assert lidar.build_pointmask_label_bytes(
        [], source_point_count=3, category_map={}
    ) == bytes(12)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "pointmask_point_count_required"),
        (True, "pointmask_point_count_invalid"),
        (3.0, "pointmask_point_count_invalid"),
        ("3", "pointmask_point_count_invalid"),
        (-1, "pointmask_point_count_invalid"),
        (4_000_001, "pointmask_point_budget_exceeded"),
    ],
)
def test_pointmask_never_infers_or_coerces_real_point_count(value, expected):
    with pytest.raises(ValueError, match=expected):
        lidar.build_pointmask_label_bytes(
            [annotation([1])], source_point_count=value, category_map={"car": 1}
        )


@pytest.mark.parametrize(
    "index,expected",
    [
        (True, "pointmask_index_invalid"),
        (1.0, "pointmask_index_invalid"),
        ("1", "pointmask_index_invalid"),
        (-1, "pointmask_index_out_of_range"),
        (3, "pointmask_index_out_of_range"),
        (2**100, "pointmask_index_out_of_range"),
    ],
)
def test_legacy_invalid_indices_are_rejected_before_any_label_allocation(
    monkeypatch, index, expected
):
    def no_allocate(*args):
        pytest.fail("invalid or enormous legacy indices must not allocate labels")

    monkeypatch.setattr(lidar, "bytearray", no_allocate, raising=False)
    with pytest.raises(ValueError, match=expected):
        lidar.build_pointmask_label_bytes(
            [annotation([index])], source_point_count=3, category_map={"car": 1}
        )


@pytest.mark.parametrize("declared", [True, 3.0, "3", -1, 4, 2**100])
def test_annotation_point_count_claim_cannot_resize_the_label_array(declared):
    with pytest.raises(ValueError, match="pointmask_point_count_mismatch"):
        lidar.build_pointmask_label_bytes(
            [annotation([], source_count=declared)],
            source_point_count=3,
            category_map={"car": 1},
        )


@pytest.mark.parametrize("indices", [None, "12", (1, 2), {1: 2}])
def test_pointmask_indices_require_an_actual_finite_list(indices):
    with pytest.raises(ValueError, match="pointmask_indices_invalid"):
        lidar.build_pointmask_label_bytes(
            [annotation(indices)], source_point_count=3, category_map={"car": 1}
        )


def test_pointmask_aggregate_index_budget_counts_duplicates_and_all_annotations(
    monkeypatch,
):
    monkeypatch.setattr(pcd, "MAX_POINTMASK_INDICES", 3)
    with pytest.raises(ValueError, match="pointmask_index_budget_exceeded"):
        lidar.build_pointmask_label_bytes(
            [annotation([1, 1]), annotation([1, 1])],
            source_point_count=3,
            category_map={"car": 1},
        )
    assert (
        len(
            lidar.build_pointmask_label_bytes(
                [annotation([1, 1, 1])], source_point_count=3, category_map={"car": 1}
            )
        )
        == 12
    )


@pytest.mark.parametrize("category_id", [0, -1, True, "1", 2**32])
def test_pointmask_category_must_be_known_and_a_nonzero_uint32(category_id):
    with pytest.raises(ValueError, match="pointmask_category_invalid"):
        lidar.build_pointmask_label_bytes(
            [annotation([])], source_point_count=3, category_map={"car": category_id}
        )
    with pytest.raises(ValueError, match="pointmask_category_unknown"):
        lidar.build_pointmask_label_bytes(
            [annotation([])], source_point_count=3, category_map={}
        )


def test_optional_annotation_claim_and_unrelated_geometry_do_not_infer_point_count():
    mask = annotation([2], source_count=None)
    other = SimpleNamespace(geometry={"type": "box_3d"}, class_name="unrelated")
    labels = lidar.build_pointmask_label_bytes(
        [other, mask], source_point_count=3, category_map={"car": 1}
    )
    assert struct.unpack("<3I", labels) == (0, 0, 1)


@pytest.mark.parametrize("geometry", [False, [], "invalid"])
def test_invalid_legacy_geometry_is_not_silently_changed_to_an_empty_mask(geometry):
    ann = SimpleNamespace(geometry=geometry, class_name="car")
    with pytest.raises(ValueError, match="pointmask_geometry_invalid"):
        lidar.build_pointmask_label_bytes(
            [ann], source_point_count=3, category_map={"car": 1}
        )


def test_pointmask_uint32_buffer_supports_full_little_endian_id_without_struct_star_args(
    monkeypatch,
):
    def no_pack(*args):
        pytest.fail("packing all labels through positional arguments is unbounded")

    monkeypatch.setattr(lidar.struct, "pack", no_pack)
    labels = lidar.build_pointmask_label_bytes(
        [annotation([1])], source_point_count=3, category_map={"car": 0xFFFFFFFF}
    )
    assert labels == bytes(4) + b"\xff" * 4 + bytes(4)
