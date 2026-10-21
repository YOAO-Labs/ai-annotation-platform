from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

import pytest

from aap_workspace.configuration_policy import ConfigurationPolicyError
from aap_workspace.guide_asset_policy import (
    ALLOWED_GUIDE_ASSET_TYPES,
    MAX_GUIDE_ASSET_SIZE_BYTES,
    assert_guide_asset_key,
    assert_guide_asset_upload_target,
    assert_registered_guide_asset_key,
    new_guide_asset_key,
    record_guide_asset_upload,
    remove_guide_asset,
    sanitize_guide_asset_filename,
)

_PROJECT_ID = UUID("f366d0a5-56aa-4152-9093-59cd5247cf01")
_ASSET_ID = UUID("76db61f5-6adc-4a23-914b-99f25f7f5201")
_OTHER_ASSET_ID = UUID("4a11850d-3988-41aa-8ee2-92221d47c028")
_KEY = f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-guide.png"
_OTHER_KEY = f"projects/{_PROJECT_ID}/guide/{_OTHER_ASSET_ID}-other.png"


def _policy_error(call, *, status_code: int, detail: str) -> None:
    with pytest.raises(ConfigurationPolicyError) as caught:
        call()
    assert caught.value.status_code == status_code
    assert caught.value.detail == detail


def test_filename_sanitization_handles_paths_unicode_controls_and_length():
    assert sanitize_guide_asset_filename("C:\\资料\\图像\n\x00.png") == "图像.png"
    assert sanitize_guide_asset_filename("folder/猫\u200b.png") == "猫.png"
    assert sanitize_guide_asset_filename("x" * 205) == "x" * 200
    assert sanitize_guide_asset_filename("folder/\x00\n\t/") == "image"


def test_new_key_keeps_mime_before_size_validation_and_legacy_size_boundary():
    assert ALLOWED_GUIDE_ASSET_TYPES == {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/svg+xml",
    }
    assert MAX_GUIDE_ASSET_SIZE_BYTES == 5 * 1024 * 1024

    key = new_guide_asset_key(
        _PROJECT_ID,
        filename="C:\\guide\\图像\n.png",
        content_type="image/png",
        size=0,
        asset_id=_ASSET_ID,
    )
    assert key == f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-图像.png"
    assert (
        new_guide_asset_key(
            _PROJECT_ID,
            filename="guide.png",
            content_type="image/png",
            size=MAX_GUIDE_ASSET_SIZE_BYTES,
            asset_id=_ASSET_ID,
        )
        == f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-guide.png"
    )

    _policy_error(
        lambda: new_guide_asset_key(
            _PROJECT_ID,
            filename="large.exe",
            content_type="application/x-msdownload",
            size=MAX_GUIDE_ASSET_SIZE_BYTES + 1,
            asset_id=_ASSET_ID,
        ),
        status_code=400,
        detail="Unsupported content type: application/x-msdownload",
    )
    _policy_error(
        lambda: new_guide_asset_key(
            _PROJECT_ID,
            filename="large.png",
            content_type="image/png",
            size=MAX_GUIDE_ASSET_SIZE_BYTES + 1,
            asset_id=_ASSET_ID,
        ),
        status_code=400,
        detail="File too large",
    )


def test_asset_key_shape_and_project_prefix_are_independent_checks():
    assert_guide_asset_key(_KEY, _PROJECT_ID)
    for invalid in (
        "outside-projects/path",
        f"projects/{_PROJECT_ID}/guide/not-a-uuid-guide.png",
        f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-nested/name.png",
        f"projects/{_PROJECT_ID}/guide/",
    ):
        _policy_error(
            lambda invalid=invalid: assert_guide_asset_key(invalid, _PROJECT_ID),
            status_code=404,
            detail="Guide asset not found",
        )

    other_project_key = f"projects/{_OTHER_ASSET_ID}/guide/{_ASSET_ID}-guide.png"
    _policy_error(
        lambda: assert_guide_asset_key(other_project_key, _PROJECT_ID),
        status_code=404,
        detail="Guide asset not found",
    )


def test_upload_target_checks_project_key_before_content_type():
    _policy_error(
        lambda: assert_guide_asset_upload_target(
            _PROJECT_ID, "foreign/key", "application/x-msdownload"
        ),
        status_code=404,
        detail="Guide asset not found",
    )
    _policy_error(
        lambda: assert_guide_asset_upload_target(
            _PROJECT_ID, _KEY, "application/x-msdownload"
        ),
        status_code=400,
        detail="Unsupported content type",
    )


def test_record_upload_returns_appended_copy_and_preserves_legacy_validation_order():
    existing = [
        {"key": _OTHER_KEY, "extra": {"source": "old"}},
        {"key": "historical malformed extras are retained", "value": [1, 2]},
    ]
    original = deepcopy(existing)
    timestamp = datetime(2026, 10, 21, 4, 5, 6, tzinfo=timezone.utc)
    entry, updated = record_guide_asset_upload(
        _PROJECT_ID,
        existing,
        key=_KEY,
        original_name="folder\\图像\n.png",
        content_type="image/png",
        size=MAX_GUIDE_ASSET_SIZE_BYTES,
        uploaded_at=timestamp,
    )

    assert existing == original
    assert updated is not existing
    assert updated[:2] == existing
    assert updated[0] is existing[0]
    assert updated[1] is existing[1]
    assert updated[2] is entry
    assert entry["key"] == _KEY
    assert entry["original_name"] == "图像.png"
    assert entry["content_type"] == "image/png"
    assert entry["uploaded_at"] == "2026-10-21T04:05:06+00:00"
    assert entry["size"] == MAX_GUIDE_ASSET_SIZE_BYTES

    _policy_error(
        lambda: record_guide_asset_upload(
            _PROJECT_ID,
            existing,
            key="foreign/key",
            original_name="bad.exe",
            content_type="application/x-msdownload",
            size=0,
            uploaded_at=timestamp,
        ),
        status_code=404,
        detail="Guide asset not found",
    )
    _policy_error(
        lambda: record_guide_asset_upload(
            _PROJECT_ID,
            existing,
            key=_KEY,
            original_name="bad.exe",
            content_type="application/x-msdownload",
            size=0,
            uploaded_at=timestamp,
        ),
        status_code=400,
        detail="Unsupported content type",
    )
    _policy_error(
        lambda: record_guide_asset_upload(
            _PROJECT_ID,
            existing,
            key=_KEY,
            original_name="empty.png",
            content_type="image/png",
            size=0,
            uploaded_at=timestamp,
        ),
        status_code=400,
        detail="Invalid upload size",
    )
    assert existing == original


def test_remove_deletes_all_duplicate_keys_and_preserves_other_order_without_mutation():
    first = {"key": _KEY, "marker": 1}
    kept = {"key": _OTHER_KEY, "marker": 2}
    duplicate = {"key": _KEY, "marker": 3}
    existing = [first, kept, duplicate]
    original = deepcopy(existing)

    result, updated = remove_guide_asset(_PROJECT_ID, existing, _KEY)

    assert result == {"deleted": _KEY}
    assert updated == [kept]
    assert updated[0] is kept
    assert existing == original
    assert existing == [first, kept, duplicate]

    _policy_error(
        lambda: remove_guide_asset(_PROJECT_ID, existing, "foreign/key"),
        status_code=404,
        detail="Guide asset not found",
    )
    _policy_error(
        lambda: remove_guide_asset(
            _PROJECT_ID,
            existing,
            f"projects/{_PROJECT_ID}/guide/{UUID(int=99)}-missing.png",
        ),
        status_code=404,
        detail="Asset not found",
    )
    assert existing == original


def test_registration_requires_project_key_and_existing_entry():
    entries = [{"key": _KEY, "extra": "retained"}]
    original = deepcopy(entries)
    assert_registered_guide_asset_key(_PROJECT_ID, entries, _KEY)
    assert entries == original

    _policy_error(
        lambda: assert_registered_guide_asset_key(_PROJECT_ID, entries, _OTHER_KEY),
        status_code=404,
        detail="Asset not found",
    )
    _policy_error(
        lambda: assert_registered_guide_asset_key(
            _PROJECT_ID, entries, f"projects/{_OTHER_ASSET_ID}/guide/{_ASSET_ID}-x.png"
        ),
        status_code=404,
        detail="Guide asset not found",
    )
    assert entries == original


@pytest.mark.parametrize(
    ("operation", "entries", "error_type"),
    [
        ("remove", [None], AttributeError),
        ("register", ["not-a-mapping"], AttributeError),
        ("register", [{"key": []}], TypeError),
    ],
)
def test_malformed_stored_entries_keep_plain_python_failures(
    operation: str, entries: list, error_type: type[Exception]
):
    with pytest.raises(error_type):
        if operation == "remove":
            remove_guide_asset(_PROJECT_ID, entries, _KEY)
        else:
            assert_registered_guide_asset_key(_PROJECT_ID, entries, _KEY)
