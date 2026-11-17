"""Native provider eligibility must reject incomplete multipart evidence."""

from types import SimpleNamespace

import pytest

from storage_s3_contract import ContractError
from verify_native_storage_contract import upload_pages


def page(pairs, *, truncated=False, cursor=None, next_cursor=None):
    result = {
        "Bucket": "owned",
        "MaxUploads": 100,
        "IsTruncated": truncated,
        "KeyMarker": cursor[0] if cursor else "",
        "UploadIdMarker": cursor[1] if cursor else "",
        "Uploads": [{"Key": key, "UploadId": upload} for key, upload in pairs],
    }
    if next_cursor is not None:
        result.update(NextKeyMarker=next_cursor[0], NextUploadIdMarker=next_cursor[1])
    return result


def client(pages):
    remaining = iter(pages)
    return SimpleNamespace(list_multipart_uploads=lambda **_: next(remaining))


def test_rejects_overfull_terminal_response_from_fixed_minio():
    expected = {(f"key-{index}", f"upload-{index}") for index in range(121)}
    with pytest.raises(ContractError, match="upload_page_bound"):
        upload_pages(client([page(expected)]), "owned", expected)


def test_empty_after_restart_cannot_prove_upload_absence():
    expected = {("saved/pending", "durable-upload")}
    with pytest.raises(ContractError, match="upload_enumeration_incomplete"):
        upload_pages(client([page([])]), "owned", expected)


def test_same_key_uploads_resume_without_losing_the_second_identity():
    before = ("same-key", "first-upload")
    after = ("same-key", "second-upload")
    expected = {before, after}
    pages = [
        page([before], truncated=True, next_cursor=before),
        page([after], cursor=before),
    ]
    assert upload_pages(client(pages), "owned", expected) == ([1, 1], before)
    assert upload_pages(
        client([page([after], cursor=before)]), "owned", {after}, cursor=before
    ) == ([1], None)


def test_same_key_resume_rejects_a_repeated_upload_instead_of_counting_it():
    before = ("same-key", "first-upload")
    after = ("same-key", "second-upload")
    pages = [
        page([before], truncated=True, next_cursor=before),
        page([before], cursor=before),
    ]
    with pytest.raises(ContractError, match="upload_page_identity"):
        upload_pages(client(pages), "owned", {before, after})


def test_next_key_without_the_exact_upload_marker_is_not_progress():
    first = ("same-key", "first-upload")
    invalid = page([first], truncated=True, next_cursor=("same-key", "wrong-upload"))
    with pytest.raises(ContractError, match="upload_cursor_progress"):
        upload_pages(client([invalid]), "owned", {first, ("same-key", "next-upload")})
