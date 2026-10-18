"""Storage publication pins the same staged source used for metadata."""

from io import BytesIO
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from PIL import Image
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.storage import StorageService


@pytest.mark.parametrize("code", ["403", "AccessDenied", "503", "SlowDown"])
def test_head_failure_is_not_treated_as_a_missing_upload(code):
    storage = StorageService()
    storage.client = MagicMock()
    storage.client.head_object.side_effect = ClientError(
        {"Error": {"Code": code}}, "HeadObject"
    )
    with pytest.raises(ClientError):
        storage.verify_upload("staged-source", bucket="imports")


def test_missing_head_and_conditional_publication():
    storage = StorageService()
    storage.client = MagicMock()
    storage.client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    assert storage.verify_upload("missing", bucket="imports") is None
    storage.publish_dataset_upload("staged", "final", etag='"digest"')
    storage.client.copy_object.assert_called_once_with(
        Bucket=storage.datasets_bucket,
        Key="final",
        CopySource={"Bucket": storage.import_bucket, "Key": "staged"},
        CopySourceIfMatch='"digest"',
    )


def test_dimension_read_pins_etag_and_closes_response_body():
    image = BytesIO()
    Image.new("RGB", (20, 30)).save(image, format="PNG")

    class ImageBody(BytesIO):
        def read(self, size=-1):
            assert size == 256 * 1024
            return super().read(size)

    body = ImageBody(image.getvalue())
    storage = StorageService()
    storage.client = MagicMock()
    storage.client.get_object.return_value = {"Body": body}
    assert storage.read_image_dimensions(
        "staged", bucket="imports", if_match='"digest"'
    ) == (20, 30)
    assert storage.client.get_object.call_args.kwargs["IfMatch"] == '"digest"'
    assert body.closed


@pytest.mark.parametrize("code", ["PreconditionFailed", "NoSuchKey", "AccessDenied"])
def test_conditional_dimension_read_propagates_storage_failure(code):
    storage = StorageService()
    storage.client = MagicMock()
    storage.client.get_object.side_effect = ClientError(
        {"Error": {"Code": code}}, "GetObject"
    )
    with pytest.raises(ClientError):
        storage.read_image_dimensions(
            "source", bucket="datasets", if_match='"original"'
        )
    assert storage.read_image_dimensions("source", bucket="datasets") is None


def test_cleanup_lease_cannot_expire_before_the_writer_hard_limit():
    with pytest.raises(ValidationError, match="hard time limit"):
        Settings(
            _env_file=None,
            image_pyramid_job_timeout_seconds=10,
            image_pyramid_lease_seconds=70,
        )
    settings = Settings(
        _env_file=None,
        image_pyramid_job_timeout_seconds=10,
        image_pyramid_lease_seconds=71,
    )
    assert settings.image_pyramid_lease_seconds == 71
