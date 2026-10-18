from __future__ import annotations

import io
import zipfile

import pytest

import aap_assets_source.archive as archive_module
from aap_assets_source.archive import (
    ArchiveLimits,
    ArchiveSafetyError,
    SafeZipArchive,
    normalize_archive_path,
)


def _zip_bytes(names: list[str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, b"content")
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../escape.txt", "archive_path_traversal"),
        ("/absolute.txt", "archive_path_absolute"),
        ("C:/absolute.txt", "archive_path_absolute"),
        ("a\\b.txt", "archive_path_invalid"),
        ("", "archive_path_invalid"),
    ],
)
def test_archive_paths_reject_traversal_and_nonportable_separators(name, code):
    with pytest.raises(ArchiveSafetyError) as caught:
        normalize_archive_path(name)
    assert caught.value.code == code


def test_archive_rejects_duplicate_casefold_and_symlink_entries():
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(_zip_bytes(["same.txt", "same.txt"])), ArchiveLimits()
        )
    assert caught.value.code == "archive_duplicate_path"

    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(_zip_bytes(["A/file.txt", "a/file.txt"])), ArchiveLimits()
        )
    assert caught.value.code == "archive_casefold_collision"

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "target")
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(io.BytesIO(output.getvalue()), ArchiveLimits())
    assert caught.value.code == "archive_symlink"


def test_archive_bounds_central_directory_before_zipfile_materializes_entries(
    monkeypatch,
):
    payload = _zip_bytes([f"{index}.txt" for index in range(4)])

    def forbidden_zipfile(*_args, **_kwargs):
        pytest.fail("ZipFile must not materialize an over-limit directory")

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(payload),
            ArchiveLimits(max_central_directory_entries=3),
        )
    assert caught.value.code == "resource_budget_exceeded"
    assert caught.value.detail["budget"] == "max_central_directory_entries"


def test_central_directory_preflight_handles_zip64(monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("large-enough-for-zip64.bin", b"zip64 payload")

    with SafeZipArchive(io.BytesIO(payload.getvalue()), ArchiveLimits()) as archive:
        assert [entry.normalized_path for entry in archive.entries] == [
            "large-enough-for-zip64.bin"
        ]


def test_skip_unsafe_paths_records_raw_names_and_excludes_members():
    payload = _zip_bytes(["safe.txt", "__MACOSX/._safe.txt", "../unsafe.txt"])
    with SafeZipArchive(
        io.BytesIO(payload), ArchiveLimits(), skip_unsafe_paths=True
    ) as archive:
        assert [entry.normalized_path for entry in archive.entries] == [
            "safe.txt",
            "__MACOSX/._safe.txt",
        ]
        assert archive.skipped_paths == ["../unsafe.txt"]


def test_archive_enforces_streamed_file_count_and_expanded_size_limits():
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(_zip_bytes(["one.txt", "two.txt"])),
            ArchiveLimits(max_files=1),
        )
    assert caught.value.detail["budget"] == "max_files"

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("zeros.bin", b"\0" * 1024)
    with pytest.raises(ArchiveSafetyError) as caught:
        SafeZipArchive(
            io.BytesIO(payload.getvalue()),
            ArchiveLimits(max_entry_bytes=100, max_compression_ratio=1000),
        )
    assert caught.value.detail["budget"] == "max_entry_bytes"


def test_archive_manifest_closure_and_copy_cleanup(tmp_path):
    payload = _zip_bytes(["nested/file.bin"])
    with SafeZipArchive(io.BytesIO(payload), ArchiveLimits()) as archive:
        archive.require_paths(["nested/file.bin"])
        with pytest.raises(ArchiveSafetyError) as caught:
            archive.require_paths(["missing.bin"])
        assert caught.value.code == "manifest_reference_missing"
        target = tmp_path / "out" / "file.bin"
        assert archive.copy_to("nested/file.bin", target) == len(b"content")
        assert target.read_bytes() == b"content"

    corrupt = io.BytesIO()
    with zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.bin", b"checksum-protected")
    damaged = bytearray(corrupt.getvalue())
    payload_offset = damaged.index(b"checksum-protected")
    damaged[payload_offset] ^= 0x01
    target = tmp_path / "corrupt.bin"
    with SafeZipArchive(io.BytesIO(damaged), ArchiveLimits()) as archive:
        with pytest.raises(zipfile.BadZipFile):
            archive.copy_to("payload.bin", target)
    assert not target.exists()
