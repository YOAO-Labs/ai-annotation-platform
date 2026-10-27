from __future__ import annotations

from io import BytesIO

from botocore.exceptions import ClientError
from PIL import Image
import pytest

from aap_assets_source.contract import SourceFailure
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime.storage import SourceStorage


def _config():
    return load_config(
        {
            "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
            "ASSETS_SOURCE_TOKEN": "s" * 40,
            "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
            "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
            "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
            "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
            "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
            "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
        }
    )


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


class _S3:
    def __init__(self) -> None:
        self.head_error: ClientError | None = None
        self.get_error: ClientError | None = None
        self.copy_error: ClientError | None = None
        self.head_result = {"ContentLength": 3, "ETag": '"etag"'}
        self.copy_call = None
        self.get_call = None
        self.body = None

    def head_object(self, **kwargs):
        self.head_call = kwargs
        if self.head_error:
            raise self.head_error
        return self.head_result

    def copy_object(self, **kwargs):
        self.copy_call = kwargs
        if self.copy_error:
            raise self.copy_error

    def get_object(self, **kwargs):
        self.get_call = kwargs
        if self.get_error:
            raise self.get_error
        return {"Body": self.body}


class _Signer:
    def __init__(self) -> None:
        self.call = None

    def generate_presigned_url(self, method, *, Params, ExpiresIn):
        self.call = method, Params, ExpiresIn
        return "https://storage.example.test/imports/upload?signature=opaque"


@pytest.mark.asyncio
async def test_presign_is_bound_to_import_bucket_content_type_and_public_origin() -> (
    None
):
    s3 = _S3()
    signer = _Signer()
    storage = SourceStorage(_config(), internal_client=s3, public_signer=signer)

    url = await storage.generate_upload_url(
        "dataset-uploads/00000000-0000-0000-0000-000000000001/source",
        "image/png",
    )

    assert url.startswith("https://storage.example.test/")
    assert signer.call == (
        "put_object",
        {
            "Bucket": "imports",
            "Key": "dataset-uploads/00000000-0000-0000-0000-000000000001/source",
            "ContentType": "image/png",
        },
        900,
    )


@pytest.mark.asyncio
async def test_only_confirmed_missing_is_missing_and_forbidden_storage_fails_closed() -> (
    None
):
    s3 = _S3()
    storage = SourceStorage(_config(), internal_client=s3, public_signer=_Signer())
    s3.head_error = _client_error("NoSuchKey", 404)
    assert await storage.verify_staging_upload("dataset-uploads/item/source") is None
    assert s3.head_call == {"Bucket": "imports", "Key": "dataset-uploads/item/source"}

    s3.head_error = _client_error("AccessDenied", 403)
    with pytest.raises(SourceFailure) as error:
        await storage.verify_staging_upload("dataset-uploads/item/source")
    assert error.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_image_dimensions_use_bounded_range_match_etag_and_exif_orientation() -> (
    None
):
    image = Image.new("RGB", (11, 19), color="white")
    exif = Image.Exif()
    exif[274] = 6
    encoded = BytesIO()
    image.save(encoded, format="JPEG", exif=exif)

    s3 = _S3()
    body = BytesIO(encoded.getvalue())
    s3.body = body
    storage = SourceStorage(_config(), internal_client=s3, public_signer=_Signer())

    assert await storage.read_staging_image_dimensions(
        "dataset-uploads/item/source", if_match='"etag"'
    ) == (19, 11)
    assert s3.get_call == {
        "Bucket": "imports",
        "Key": "dataset-uploads/item/source",
        "Range": "bytes=0-262143",
        "IfMatch": '"etag"',
    }
    assert body.closed


@pytest.mark.asyncio
async def test_forbidden_range_read_is_storage_failure_not_missing() -> None:
    s3 = _S3()
    s3.get_error = _client_error("AccessDenied", 403)
    storage = SourceStorage(_config(), internal_client=s3, public_signer=_Signer())

    with pytest.raises(SourceFailure) as error:
        await storage.read_staging_image_dimensions(
            "dataset-uploads/item/source", if_match='"etag"'
        )
    assert error.value.code == "storage_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status", [("PreconditionFailed", 412), ("NoSuchKey", 404)]
)
async def test_conditioned_image_read_rejects_replaced_or_removed_source(code, status):
    s3 = _S3()
    s3.get_error = _client_error(code, status)
    storage = SourceStorage(_config(), internal_client=s3, public_signer=_Signer())
    for read in (
        storage.read_staging_image_dimensions,
        storage.read_legacy_image_dimensions,
    ):
        with pytest.raises(SourceFailure) as error:
            await read("source/key", if_match='"original-etag"')
        assert error.value.code == "source_changed"


def test_storage_has_no_object_delete_or_bucket_management_surface() -> None:
    assert not hasattr(SourceStorage, "delete_object")
    assert not hasattr(SourceStorage, "delete_prefix")
    assert not hasattr(SourceStorage, "ensure_bucket")
    assert not hasattr(SourceStorage, "list_objects")
