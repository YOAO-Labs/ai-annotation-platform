from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_images.contract import (
    ImageBackfillPage,
    ImageBackfillSelection,
    ImageFailure,
    ImageSource,
    ScanImageBackfillRequest,
)
from aap_assets_images.http_contract import http_document
from aap_assets_images.transport import (
    ImageAssetReferences,
    ImageAssetReferencesRequest,
    ImageGenerationReference,
    ImageGenerationReferenceRequest,
    ImagePyramidSummary,
    ImageSourceReference,
    iter_image_backfill_sources,
    require_asset_reference_scope,
    require_generation_reference_scope,
    require_source_reference_scope,
)


def source(item_id=None, *, dataset_id=None):
    item_id = item_id or uuid4()
    return ImageSource(
        owner={"kind": "dataset_item", "id": item_id},
        task_id=None,
        project_id=uuid4(),
        dataset_id=dataset_id or uuid4(),
        item_id=item_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "e", "byte_size": 12},
        source_drifted=False,
        upload_ready=True,
        file_path="dataset/file.png",
        file_name="file.png",
        file_type="image",
        bucket_kind="datasets",
        file_size=12,
        content_hash=None,
        width=20,
        height=10,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


def test_reference_requests_use_logical_coordinates_and_stay_bounded():
    selector = dict(actor_id=uuid4(), dataset_item_id=uuid4())
    request = ImageAssetReferencesRequest(
        **selector,
        items=[
            {"kind": "overview", "generation": 4},
            {"kind": "tile", "generation": 4, "level": 1, "x": 2, "y": 3},
        ],
    )
    assert len(request.items) == 2
    with pytest.raises(ValidationError):
        ImageAssetReferencesRequest(
            **selector,
            items=[
                {
                    "kind": "tile",
                    "generation": 4,
                    "level": 1,
                    "x": 2,
                    "y": 3,
                    "key": "other/file.webp",
                }
            ],
        )
    with pytest.raises(ValidationError):
        ImageAssetReferencesRequest(
            **selector,
            items=[{"kind": "overview", "generation": i + 1} for i in range(129)],
        )
    with pytest.raises(ValidationError):
        ImageAssetReferencesRequest(
            **selector,
            items=[
                {"kind": "overview", "generation": 4},
                {"kind": "overview", "generation": 5},
            ],
        )


def test_asset_reference_reply_preserves_every_requested_locator():
    item = uuid4()
    current = source(item)
    request = ImageAssetReferencesRequest(
        actor_id=uuid4(),
        dataset_item_id=item,
        expected_source_snapshot=current.source_snapshot,
        items=[
            {"kind": "overview", "generation": 2},
            {"kind": "tile", "generation": 2, "level": 0, "x": 1, "y": 2},
        ],
    )
    reply = ImageAssetReferences(
        source=current,
        generation=2,
        expires_in=120,
        items=[
            {
                "kind": "overview",
                "generation": 2,
                "level": None,
                "x": None,
                "y": None,
                "url": "https://media.local/o",
            },
            {
                "kind": "tile",
                "generation": 2,
                "level": 0,
                "x": 1,
                "y": 2,
                "url": "https://media.local/t",
            },
        ],
    )
    require_asset_reference_scope(request, reply)
    with pytest.raises(ValueError, match="escaped"):
        require_asset_reference_scope(
            request, reply.model_copy(update={"source": source(uuid4())})
        )
    with pytest.raises(ValueError, match="escaped"):
        require_asset_reference_scope(
            request,
            reply.model_copy(update={"items": [reply.items[0], reply.items[0]]}),
        )


def test_signed_reference_urls_reject_credentials_fragments_and_non_http():
    current = source()
    with pytest.raises(ValidationError):
        ImageSourceReference(
            source=current,
            original_url="https://user:password@media.local/file",
            thumbnail_url=None,
            expires_in=60,
        )
    with pytest.raises(ValidationError):
        ImageSourceReference(
            source=current,
            original_url="file:///tmp/image.png",
            thumbnail_url=None,
            expires_in=60,
        )


def test_source_and_generation_references_cannot_cross_actor_or_snapshot():
    actor_id = uuid4()
    item_id = uuid4()
    current = source(item_id)
    request = ImageGenerationReferenceRequest(
        actor_id=actor_id,
        dataset_item_id=item_id,
        expected_source_snapshot=current.source_snapshot,
    )
    summary = ImagePyramidSummary(
        status="missing",
        required=False,
        generation=None,
        building_generation=None,
        building_status=None,
        width=None,
        height=None,
        format=None,
    )
    reference = ImageGenerationReference(
        source=current,
        pyramid=summary,
        pyramid_source_generation=None,
        pyramid_source_snapshot=None,
        manifest=None,
        overview_url=None,
        expires_in=120,
    )
    require_generation_reference_scope(request, reference)
    with pytest.raises(ValueError, match="escaped"):
        require_generation_reference_scope(
            request,
            reference.model_copy(update={"source": source(uuid4())}),
        )
    source_reference = ImageSourceReference(
        actor_id=actor_id,
        source=current,
        original_url="https://media.local/original",
        thumbnail_url=None,
        expires_in=60,
    )
    from aap_assets_images.contract import ReadImageSourceRequest

    require_source_reference_scope(
        ReadImageSourceRequest(actor_id=actor_id, dataset_item_id=item_id),
        source_reference,
    )
    with pytest.raises(ValueError, match="escaped"):
        require_source_reference_scope(
            ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=item_id),
            source_reference,
        )


@pytest.mark.asyncio
async def test_backfill_iterator_checks_scope_snapshot_and_global_cursor_progress():
    dataset_id = uuid4()
    first = source(dataset_id=dataset_id)
    second = source(dataset_id=dataset_id)
    selection = ImageBackfillSelection(
        owner_kind="dataset_item", dataset_id=dataset_id, purpose="pyramid"
    )
    request = ScanImageBackfillRequest(selection=selection, page_size=1)
    calls = []
    pages_cutoff = datetime.now(timezone.utc)

    class Pages:
        async def call(self, operation, page_request):
            calls.append(
                (
                    operation,
                    page_request.after_owner_id,
                    page_request.selection.accepted_before,
                )
            )
            if page_request.after_owner_id is None:
                row = first if first.owner.id.int < second.owner.id.int else second
                return ImageBackfillPage(
                    items=[row],
                    snapshot_digest="f" * 64,
                    total=2,
                    accepted_before=selection.accepted_before or pages_cutoff,
                    after_owner_kind=None,
                    after_owner_id=None,
                    next_owner_kind=row.owner.kind,
                    next_owner_id=row.owner.id,
                    complete=False,
                )
            row = second if first.owner.id.int < second.owner.id.int else first
            return ImageBackfillPage(
                items=[row],
                snapshot_digest="f" * 64,
                total=2,
                accepted_before=selection.accepted_before or pages_cutoff,
                after_owner_kind=page_request.after_owner_kind,
                after_owner_id=page_request.after_owner_id,
                next_owner_kind=None,
                next_owner_id=None,
                complete=True,
            )

    yielded = [row async for row in iter_image_backfill_sources(Pages(), request)]
    assert len(yielded) == 2
    assert calls[0] == ("scan_image_backfill", None, None)
    assert calls[1][1] == yielded[0].owner.id
    assert calls[1][2] == pages_cutoff

    class ChangedPages:
        count = 0
        ordered = sorted((first, second), key=lambda row: row.owner.id.int)

        async def call(self, operation, page_request):
            self.count += 1
            row = self.ordered[self.count - 1]
            return ImageBackfillPage(
                items=[row],
                snapshot_digest=("a" if self.count == 1 else "b") * 64,
                total=2,
                accepted_before=pages_cutoff,
                after_owner_kind=page_request.after_owner_kind,
                after_owner_id=page_request.after_owner_id,
                next_owner_kind=row.owner.kind if self.count == 1 else None,
                next_owner_id=row.owner.id if self.count == 1 else None,
                complete=self.count != 1,
            )

    with pytest.raises(ImageFailure, match="source_changed"):
        [row async for row in iter_image_backfill_sources(ChangedPages(), request)]


def test_shipped_http_image_namespace_has_closed_refs_and_no_worker_commands():
    document = http_document()
    path = Path(__file__).parents[1] / "src/aap_assets_images/openapi.snapshot.json"
    assert json.loads(path.read_text()) == document
    assert "/v1/images/commands/claim_image_media" not in document["paths"]
    assert "/v1/images/commands/claim_image_garbage" not in document["paths"]
    assert len(document["paths"]) == 20

    def inspect(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name == "$ref":
                    assert item.startswith("#/components/schemas/")
                    assert item.rsplit("/", 1)[1] in document["components"]["schemas"]
                else:
                    inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(document)
