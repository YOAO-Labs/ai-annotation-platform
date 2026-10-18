from __future__ import annotations

import httpx
import pytest

from aap_assets_media import contract as c
from aap_assets_media.transport import SourceReference
from aap_assets_media_runtime.http_source import HttpMediaSourceStorage
from test_contract import source


@pytest.mark.asyncio
async def test_remote_cli_download_uses_owner_reference_without_service_token(tmp_path):
    data = b"frozen-bytes"
    identity = c.ObjectIdentity(etag="source-etag", byte_size=len(data))
    s = source(object_identity=identity)

    class Owner:
        async def internal_source_reference(self, request):
            assert request.dataset_item_id == s.dataset_item_id
            return SourceReference(
                source=s,
                original_url="http://objects.local/datasets/source.mp4?signature=source-only",
                video_url="http://objects.local/datasets/source.mp4",
                poster_url=None,
                expires_in=3600,
            )

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data

    def object_store(request):
        assert request.url.host == "objects.local"
        assert "authorization" not in request.headers
        assert request.headers["if-match"] == '"source-etag"'
        return httpx.Response(200, headers={"ETag": '"source-etag"'}, stream=Bytes())

    storage = HttpMediaSourceStorage(
        Owner(), s, transport=httpx.MockTransport(object_store)
    )
    assert await storage.head_source(s.file_path) == identity
    output = tmp_path / "download.mp4"
    await storage.download_source(s, output)
    assert output.read_bytes() == data


@pytest.mark.asyncio
async def test_remote_cli_reference_replacement_fails_before_object_read():
    s = source()

    class Owner:
        async def internal_source_reference(self, request):
            return SourceReference(
                source=s.model_copy(update={"source_snapshot": "b" * 64}),
                original_url="http://objects.local/datasets/new.mp4",
                video_url="http://objects.local/datasets/new.mp4",
                poster_url=None,
                expires_in=3600,
            )

    with pytest.raises(c.MediaFailure, match="source_changed"):
        await HttpMediaSourceStorage(Owner(), s).head_source(s.file_path)
