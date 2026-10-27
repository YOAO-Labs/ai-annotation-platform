from concurrent.futures import ThreadPoolExecutor
import os
import stat

import pytest

from aap_protocol_v2.managed_credential import (
    CredentialFileError,
    ensure_credential,
    read_credential,
)


def test_initialize_once_is_private_persistent_and_concurrent(tmp_path):
    directory = tmp_path / "auth"
    directory.mkdir(mode=0o755)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: ensure_credential(directory), range(32)))
    assert len(set(tokens)) == 1
    assert len(tokens[0]) == 64
    assert read_credential(directory) == tokens[0]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "token").stat().st_mode) == 0o600
    assert {p.name for p in directory.iterdir()} == {"token"}


@pytest.mark.parametrize(
    "invalid", [b"short", b"x" * 64, b"a" * 65, b"a" * 64 + b"\nextra"]
)
def test_invalid_existing_file_is_never_overwritten(tmp_path, invalid):
    tmp_path.chmod(0o700)
    path = tmp_path / "token"
    path.write_bytes(invalid)
    path.chmod(0o600)
    with pytest.raises(CredentialFileError):
        ensure_credential(tmp_path)
    assert path.read_bytes() == invalid


@pytest.mark.parametrize(
    "kind", ["file-symlink", "directory-symlink", "fifo", "public-file"]
)
def test_unsafe_store_fails_closed(tmp_path, kind):
    directory = tmp_path / "auth"
    directory.mkdir(mode=0o700)
    path = directory / "token"
    if kind == "directory-symlink":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    elif kind == "file-symlink":
        target = tmp_path / "original"
        target.write_text("a" * 64)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        path.write_text("a" * 64)
        path.chmod(0o644)
    with pytest.raises((CredentialFileError, OSError)):
        ensure_credential(directory)


def test_read_never_initializes_or_repairs_directory(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        read_credential(path)
    assert not path.exists()
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    with pytest.raises(CredentialFileError):
        read_credential(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
