from __future__ import annotations

import os
import stat

import pytest

from ai_annotation._plugin_receipt import ReceiptStore
from ai_annotation.errors import AAPError


def test_receipt_is_private_and_survives_reopen(tmp_path):
    store = ReceiptStore(tmp_path / "instance")
    with store.locked(create=True):
        assert store.load() is None
        store.save({"installation_id": "one", "token": "secret"})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    with ReceiptStore(store.directory).locked() as reopened:
        assert reopened.load() == {"installation_id": "one", "token": "secret"}


def test_receipt_refuses_unsafe_permissions_and_symlinks(tmp_path):
    directory = tmp_path / "instance"
    store = ReceiptStore(directory)
    with store.locked(create=True):
        store.save({"installation_id": "one"})
    os.chmod(store.path, 0o644)
    with store.locked(), pytest.raises(AAPError):
        store.load()
    store.path.unlink()
    target = tmp_path / "other"
    target.write_text('{}')
    os.chmod(target, 0o600)
    store.path.symlink_to(target)
    with store.locked(), pytest.raises(AAPError):
        store.load()
