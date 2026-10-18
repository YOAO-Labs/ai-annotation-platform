"""A timed-out writer cannot start another pyramid write after cleanup eligibility."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.image_pyramid import ImagePyramidError
from app.workers import image_pyramid


def test_expired_writer_performs_no_object_upload(tmp_path):
    source = tmp_path / "tile.webp"
    source.write_bytes(b"tile")
    storage = MagicMock()
    with pytest.raises(ImagePyramidError):
        image_pyramid._upload_and_verify(
            storage,
            asset_id=uuid4(),
            generation_number=1,
            tiles=[(source, 0, 0, 0)],
            overview_path=source,
            manifest_bytes=b"{}",
            deadline=0,
        )
    storage.upload_file.assert_not_called()
    storage.client.put_object.assert_not_called()


def test_deadline_after_verification_prevents_manifest_publication(
    tmp_path, monkeypatch
):
    source = tmp_path / "overview.webp"
    source.write_bytes(b"overview")
    asset_id = uuid4()
    now = [0]
    monkeypatch.setattr(image_pyramid.time, "monotonic", lambda: now[0])
    storage = MagicMock()

    def listed(*args, **kwargs):
        now[0] = 11
        return [{"key": image_pyramid.overview_key(asset_id, 1), "size": 8}]

    storage.list_objects.side_effect = listed
    with pytest.raises(ImagePyramidError):
        image_pyramid._upload_and_verify(
            storage,
            asset_id=asset_id,
            generation_number=1,
            tiles=[],
            overview_path=source,
            manifest_bytes=b"{}",
            deadline=10,
        )
    storage.upload_file.assert_called_once()
    storage.client.put_object.assert_not_called()
