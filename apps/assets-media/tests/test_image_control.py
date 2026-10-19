from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from aap_assets_images import contract as c
from aap_assets_images.geometry import (
    build_manifest,
    canonical_manifest_bytes,
    sha256_digest,
)
from aap_assets_images.policy import ImagePolicy
from aap_assets_images.transport import (
    ImageAssetReferencesRequest,
    ImageGenerationReferenceRequest,
    ImageSourceReferencesRequest,
    InternalImageSourceReferenceRequest,
)
from aap_assets_images_runtime.control import ImageControl


def source(*, owner=None, thumbnail_path=None, thumbnail_current=False):
    owner = owner or c.ImageOwner(kind="dataset_item", id=uuid4())
    is_item = owner.kind == "dataset_item"
    return c.ImageSource(
        owner=owner,
        task_id=None if is_item else owner.id,
        project_id=uuid4(),
        dataset_id=uuid4() if is_item else None,
        item_id=owner.id if is_item else None,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "source-etag", "byte_size": 128},
        source_drifted=False,
        upload_ready=True,
        file_path="source/image.png",
        file_name="image.png",
        file_type="image",
        bucket_kind="datasets" if is_item else "legacy",
        file_size=128,
        content_hash=None,
        width=2,
        height=1,
        thumbnail_path=thumbnail_path,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj" if thumbnail_path else None,
        thumbnail_source_generation=1 if thumbnail_current else None,
        thumbnail_error=None,
    )


def accepted_for(request, current, *, status="pending"):
    if request.purpose == "dimensions":
        spec = c.DimensionsWorkSpec(
            expected_source_snapshot=current.source_snapshot, policy=request.policy
        )
    elif request.purpose == "thumbnail":
        spec = c.ThumbnailWorkSpec(
            expected_source_snapshot=current.source_snapshot, policy=request.policy
        )
    else:
        spec = c.PyramidWorkSpec(
            expected_source_snapshot=current.source_snapshot, policy=request.policy
        )
    return c.AcceptedImageWork(
        operation_id=request.operation_id,
        source=current,
        work=c.ImageWorkReference(
            job_id=uuid4(),
            generation=1,
            owner=current.owner,
            source_generation=current.source_generation,
            kind=request.purpose,
            status=status,
        ),
        spec=spec,
    )


class FakeStorage:
    def __init__(self, *, cache_keys=(), cache_bytes=None, source_identity=None):
        self.cache_keys = set(cache_keys)
        self.cache_bytes = dict(cache_bytes or {})
        self.source_identity = source_identity
        self.head_source_calls = []
        self.head_cache_calls = []
        self.read_cache_calls = []
        self.signed_source = []
        self.signed_cache = []

    async def head_source(self, current):
        self.head_source_calls.append(current)
        return self.source_identity or current.object_identity

    async def head_cache(self, key):
        self.head_cache_calls.append(key)
        if key not in self.cache_keys and key not in self.cache_bytes:
            return None
        return c.ObjectIdentity(etag="cache-etag", byte_size=64)

    async def read_cache(self, key, limit):
        self.read_cache_calls.append((key, limit))
        value = self.cache_bytes.get(key)
        if value is None:
            raise FileNotFoundError(key)
        if len(value) > limit:
            raise ValueError("too large")
        return value

    async def sign_source(self, current, expires_in=3600):
        self.signed_source.append((current, expires_in))
        return f"https://image.local/{current.bucket_kind}/{current.file_path}"

    async def sign_cache(self, key, expires_in=3600):
        self.signed_cache.append((key, expires_in))
        return f"https://image.local/cache/{key}?expires={expires_in}"


@pytest.mark.asyncio
async def test_image_control_closes_optional_async_storage_client():
    class CloseableStorage:
        closed = False

        async def close(self):
            self.closed = True

    storage = CloseableStorage()
    await ImageControl(None, storage).close()
    assert storage.closed


@pytest.mark.asyncio
async def test_typed_missing_source_preserves_the_owner_source_error():
    class MissingStorage:
        async def head_source(self, _source):
            raise c.ImageFailure("object_missing")

    control = ImageControl(None, MissingStorage())
    with pytest.raises(c.ImageFailure, match="source_missing"):
        await control._storage_call("head_source", None, missing_code="source_missing")
    with pytest.raises(c.ImageFailure, match="object_missing"):
        await control._storage_call("head_source", None)


@pytest.mark.asyncio
async def test_source_batch_settles_peer_reads_before_reporting_a_leaf_failure(
    monkeypatch,
):
    failed, peer = uuid4(), uuid4()
    settled = []
    control = ImageControl(None, FakeStorage())

    async def read(request):
        if request.task_id == failed:
            raise c.ImageFailure("source_missing")
        await asyncio.sleep(0.01)
        settled.append(request.task_id)

    monkeypatch.setattr(control, "source_reference", read)
    with pytest.raises(c.ImageFailure, match="source_missing"):
        await control.source_references(
            ImageSourceReferencesRequest(
                requests=[
                    c.ReadImageSourceRequest(actor_id=uuid4(), task_id=task_id)
                    for task_id in (failed, peer)
                ]
            )
        )
    assert settled == [peer]


class Database:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def call(self, operation, request):
        self.calls.append((operation, request))
        result = self.handler(operation, request)
        if hasattr(result, "__await__"):
            return await result
        return result


@pytest.mark.asyncio
async def test_public_observation_binds_current_source_without_signing_or_dispatch():
    current = source(owner=c.ImageOwner(kind="task", id=uuid4()))
    unbound = current.model_copy(update={"object_identity": None})

    def native(operation, request):
        if operation == "read_image_source":
            return unbound
        assert operation == "observe_image_source"
        assert request.object_identity == current.object_identity
        return current

    database = Database(native)
    storage = FakeStorage(source_identity=current.object_identity)
    observed = await ImageControl(database, storage).observe_source(
        c.ReadImageSourceRequest(actor_id=uuid4(), task_id=current.task_id),
        expected_source_snapshot=current.source_snapshot,
    )
    assert observed == current
    assert storage.head_source_calls == [unbound]
    assert not storage.signed_source and not storage.signed_cache


@pytest.mark.asyncio
async def test_observation_rejects_captured_snapshot_change_before_head():
    current = source(owner=c.ImageOwner(kind="task", id=uuid4()))
    database = Database(lambda _operation, _request: current)
    storage = FakeStorage()

    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageControl(database, storage).observe_source(
            c.ReadImageSourceRequest(actor_id=uuid4(), task_id=current.task_id),
            expected_source_snapshot="f" * 64,
        )

    assert [operation for operation, _request in database.calls] == [
        "read_image_source"
    ]
    assert storage.head_source_calls == []


def pyramid_projection(current, *, status="ready", generation=2, attempt=None):
    asset_id = uuid4()
    attempt = attempt or uuid4()
    base = f"image-pyramids/{asset_id}/g{generation}/a{attempt}"
    manifest_data = build_manifest(
        generation=generation,
        source_fingerprint="sha256:" + "b" * 64,
        width=2,
        height=1,
        overview_width=2,
        overview_height=1,
        overview_digest="sha256:" + "c" * 64,
        policy=ImagePolicy(),
    )
    raw = canonical_manifest_bytes(manifest_data)
    manifest = c.ImagePyramidManifest.model_validate(manifest_data)
    row = c.ImagePyramidGeneration(
        asset_id=asset_id,
        status=status,
        required=False,
        generation=generation,
        building_generation=None,
        building_status=None,
        source_generation=current.source_generation,
        source_snapshot=current.source_snapshot,
        width=2,
        height=1,
        format="webp",
        manifest=manifest if status == "ready" else None,
        manifest_key=f"{base}/manifest.json" if status == "ready" else None,
        manifest_digest=sha256_digest(raw) if status == "ready" else None,
        overview_key=f"{base}/overview.webp" if status == "ready" else None,
        overview_digest=manifest.overview.content_digest if status == "ready" else None,
        tile_count=2 if status == "ready" else 0,
        retained_bytes=43 if status == "ready" else 0,
    )
    state = c.ImageGenerationRead(source=current, pyramid=row)
    return state, raw, base


def retained_thumbnail_work_status(
    current,
    key,
    *,
    job_id,
    generation,
    refresh_generation,
    status,
    publication_source=None,
):
    publication_source = publication_source or current
    policy = ImagePolicy()
    publication_spec = c.ThumbnailWorkSpec(
        expected_source_snapshot=publication_source.source_snapshot, policy=policy
    )
    result = c.ThumbnailResult(
        source_identity=publication_source.object_identity,
        source_sha256="a" * 64,
        width=publication_source.width,
        height=publication_source.height,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        artifact=c.Artifact(key=key, byte_size=64, sha256="b" * 64),
    )
    publication = {
        "schema": "aap-image-thumbnail-publication/v1",
        "job_id": str(job_id),
        "generation": generation,
        "owner": publication_source.owner.model_dump(mode="json"),
        "source_generation": publication_source.source_generation,
        "source_snapshot": publication_source.source_snapshot,
        "source_identity": publication_source.object_identity.model_dump(mode="json"),
        "spec": publication_spec.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }
    return c.ImageWorkStatus(
        work=c.ImageWorkReference(
            job_id=job_id,
            generation=refresh_generation,
            owner=current.owner,
            source_generation=current.source_generation,
            kind="thumbnail",
            status=status,
        ),
        spec=c.ThumbnailWorkSpec(
            expected_source_snapshot=current.source_snapshot, policy=policy
        ),
        progress={"retained_publication": publication},
        error_code=None,
        error_detail=None,
        lease_expires_at=None,
    )


@pytest.mark.asyncio
async def test_source_drift_is_observed_before_any_url_is_signed():
    current = source()
    changed = current.model_copy(update={"source_drifted": True})

    class DriftDatabase:
        async def call(self, operation, request):
            return current if operation == "read_image_source" else changed

    storage = FakeStorage()
    request = c.ReadImageSourceRequest(
        actor_id=uuid4(), dataset_item_id=current.item_id
    )
    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageControl(DriftDatabase(), storage).source_reference(request)
    assert storage.signed_source == []
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_actor_source_first_read_can_bind_an_unregistered_identity():
    unbound = source().model_copy(update={"object_identity": None})
    identity = c.ObjectIdentity(etag="first-observation", byte_size=128)
    bound = unbound.model_copy(update={"object_identity": identity})
    reads = 0

    class BindingDatabase:
        async def call(self, operation, request):
            nonlocal reads
            if operation == "read_image_source":
                reads += 1
                return unbound if reads == 1 else bound
            if operation == "prepare_image_media":
                return accepted_for(request, bound)
            assert operation == "observe_image_source"
            return bound

    storage = FakeStorage(source_identity=identity)
    result = await ImageControl(BindingDatabase(), storage).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=unbound.item_id)
    )
    assert result.source == bound
    assert storage.signed_source == [(bound, 3600)]


@pytest.mark.asyncio
async def test_internal_source_first_read_can_bind_an_unregistered_identity():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    unbound = source(owner=owner).model_copy(update={"object_identity": None})
    identity = c.ObjectIdentity(etag="first-internal-observation", byte_size=128)
    bound = unbound.model_copy(update={"object_identity": identity})
    reads = 0

    class BindingDatabase:
        async def call(self, operation, request):
            nonlocal reads
            assert operation == "resolve_internal_image_source"
            reads += 1
            return unbound if reads == 1 else bound

    storage = FakeStorage(source_identity=identity)
    result = await ImageControl(BindingDatabase(), storage).internal_source_reference(
        InternalImageSourceReferenceRequest(
            source=c.InternalImageSourceRequest(owner=owner)
        )
    )
    assert result.source == bound
    assert result.actor_id is None
    assert storage.signed_source == [(bound, 3600)]


@pytest.mark.asyncio
async def test_source_change_after_signing_is_committed_before_response_is_rejected():
    current = source()
    changed_identity = c.ObjectIdentity(etag="replacement-etag", byte_size=128)
    changed = current.model_copy(
        update={"source_drifted": True, "object_identity": changed_identity}
    )
    observed = 0

    class ChangingDatabase:
        async def call(self, operation, request):
            nonlocal observed
            if operation == "read_image_source":
                return current
            assert operation == "observe_image_source"
            observed += 1
            return current if observed == 1 else changed

    class ChangingStorage(FakeStorage):
        async def head_source(self, requested_source):
            self.head_source_calls.append(requested_source)
            if len(self.head_source_calls) == 1:
                return current.object_identity
            return changed_identity

    storage = ChangingStorage()
    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageControl(ChangingDatabase(), storage).raw_source_reference(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=current.item_id)
        )
    assert len(storage.signed_source) == 1


@pytest.mark.asyncio
async def test_registered_dimension_change_during_reference_blocks_the_reply():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    key = f"thumbnails/{owner.id}.webp"
    current = source(owner=owner, thumbnail_path=key, thumbnail_current=True)
    changed = current.model_copy(update={"width": current.width + 1})
    observed = 0

    class ChangingDatabase:
        async def call(self, operation, request):
            nonlocal observed
            if operation == "read_image_source":
                return current
            assert operation == "observe_image_source"
            observed += 1
            return current if observed == 1 else changed

    storage = FakeStorage(cache_keys=[key])
    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageControl(ChangingDatabase(), storage).source_reference(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
        )
    assert storage.signed_source == [(current, 3600)]
    assert storage.signed_cache == [(key, 3600)]


@pytest.mark.asyncio
async def test_stale_pyramid_from_registered_dimensions_returns_no_overview_url():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner).model_copy(update={"width": 3})
    state, _raw, _prefix = pyramid_projection(current, status="stale", generation=2)

    class StalePyramidDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            return state

    storage = FakeStorage(cache_keys=[state.pyramid.overview_key])
    result = await ImageControl(StalePyramidDatabase(), storage).generation_reference(
        ImageGenerationReferenceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )
    assert result.pyramid.status == "stale"
    assert result.manifest is None
    assert result.overview_url is None
    assert storage.signed_cache == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code", ["permission_denied", "operation_busy", "operation_conflict"]
)
async def test_unknown_dimensions_warmup_denial_keeps_generation_read_available(
    failure_code,
):
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner).model_copy(update={"width": None, "height": None})
    state = c.ImageGenerationRead(
        source=current,
        pyramid=c.ImagePyramidGeneration(
            asset_id=None,
            status="not_available",
            required=False,
            generation=None,
            building_generation=None,
            building_status=None,
            source_generation=None,
            source_snapshot=None,
            width=None,
            height=None,
            format=None,
            manifest=None,
            manifest_key=None,
            manifest_digest=None,
            overview_key=None,
            overview_digest=None,
            tile_count=0,
            retained_bytes=0,
        ),
    )
    calls = []

    class UnknownDimensionsDatabase:
        async def call(self, operation, request):
            calls.append(operation)
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return state
            if operation == "prepare_image_media":
                raise c.ImageFailure(failure_code)
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage()
    result = await ImageControl(
        UnknownDimensionsDatabase(), storage
    ).generation_reference(
        ImageGenerationReferenceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )
    assert result.source.width is None and result.source.height is None
    assert result.pyramid.status == "not_available"
    assert result.overview_url is None
    assert calls.count("prepare_image_media") == 1
    assert calls.count("read_image_generation") == 2
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_current_pyramid_required_flag_uses_control_policy_for_summaries():
    current = source()
    summary = c.ImagePyramidSummary(
        status="missing",
        required=False,
        generation=None,
        building_generation=None,
        building_status=None,
        width=None,
        height=None,
        format=None,
    )
    summaries = c.ImageSummaries(
        items=[
            c.ImageSummaryItem(
                selector=c.ImageSourceSelector(dataset_item_id=current.item_id),
                source=current,
                thumbnail_current=False,
                pyramid=summary,
            )
        ]
    )

    class SummaryDatabase:
        async def call(self, operation, request):
            assert operation == "read_image_summaries"
            return summaries

    policy = ImagePolicy(optional_pixels=1, required_pixels=1)
    result = await ImageControl(SummaryDatabase(), FakeStorage(), policy).call(
        "read_image_summaries",
        c.ReadImageSummariesRequest(
            actor_id=uuid4(),
            selectors=[c.ImageSourceSelector(dataset_item_id=current.item_id)],
        ),
    )
    assert result.items[0].pyramid.required is True


@pytest.mark.asyncio
async def test_optional_pyramid_dispatch_uses_control_policy_and_captured_spec():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    policy = ImagePolicy(
        optional_pixels=1,
        required_pixels=100,
        auto_generate_pyramids=True,
    )
    missing = c.ImagePyramidGeneration(
        asset_id=None,
        status="missing",
        required=False,
        generation=None,
        building_generation=None,
        building_status=None,
        source_generation=None,
        source_snapshot=None,
        width=None,
        height=None,
        format=None,
        manifest=None,
        manifest_key=None,
        manifest_digest=None,
        overview_key=None,
        overview_digest=None,
        tile_count=0,
        retained_bytes=0,
    )
    pending = c.ImagePyramidGeneration(
        asset_id=None,
        status="pending",
        required=False,
        generation=None,
        building_generation=1,
        building_status="pending",
        source_generation=None,
        source_snapshot=None,
        width=None,
        height=None,
        format=None,
        manifest=None,
        manifest_key=None,
        manifest_digest=None,
        overview_key=None,
        overview_digest=None,
        tile_count=0,
        retained_bytes=0,
    )
    reads = 0
    admitted = []

    class AutoPyramidDatabase:
        async def call(self, operation, request):
            nonlocal reads
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                reads += 1
                return c.ImageGenerationRead(
                    source=current, pyramid=missing if reads == 1 else pending
                )
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    request = ImageGenerationReferenceRequest(
        actor_id=uuid4(), dataset_item_id=owner.id, expires_in=60
    )
    response = await ImageControl(
        AutoPyramidDatabase(), FakeStorage(), policy
    ).generation_reference(request)
    assert response.pyramid.status == "pending"
    assert response.pyramid.required is False
    assert len(admitted) == 1
    assert admitted[0].purpose == "pyramid"
    assert admitted[0].policy == policy
    assert admitted[0].force is False


@pytest.mark.asyncio
async def test_configured_profile_selects_missing_lineage_instead_of_old_ready():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    old_ready, _old_raw, _old_prefix = pyramid_projection(current)
    old_ready = c.ImageGenerationRead(
        source=current,
        pyramid=old_ready.pyramid.model_copy(
            update={"profile_version": "pyramid-legacy"}
        ),
    )
    new_missing, _new_raw, _new_prefix = pyramid_projection(current, status="missing")
    configured_profile = "pyramid-current"
    new_missing = c.ImageGenerationRead(
        source=current,
        pyramid=new_missing.pyramid.model_copy(
            update={"profile_version": configured_profile}
        ),
    )
    policy = ImagePolicy(profile_version=configured_profile)
    states = {"pyramid-legacy": old_ready, configured_profile: new_missing}

    class ProfileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            assert request.profile_version == configured_profile
            return states[request.profile_version]

    storage = FakeStorage()
    control = ImageControl(ProfileDatabase(), storage, policy)
    result = await control.generation_reference(
        ImageGenerationReferenceRequest(
            actor_id=uuid4(), dataset_item_id=owner.id, expires_in=60
        )
    )
    direct = await control.call(
        "read_image_generation",
        c.ReadImageGenerationRequest(actor_id=uuid4(), dataset_item_id=owner.id),
    )

    assert old_ready.pyramid.status == "ready"
    assert result.pyramid.profile_version == configured_profile
    assert result.pyramid.status == "missing"
    assert result.overview_url is None
    assert direct.pyramid.profile_version == configured_profile
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_explicit_old_profile_returns_only_its_ready_generation():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, prefix = pyramid_projection(current)
    selected_profile = "pyramid-legacy"
    old_state = c.ImageGenerationRead(
        source=current,
        pyramid=state.pyramid.model_copy(update={"profile_version": selected_profile}),
    )

    class ProfileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            assert request.profile_version == selected_profile
            return old_state

    storage = FakeStorage(
        cache_keys=[f"{prefix}/overview.webp"],
        cache_bytes={old_state.pyramid.manifest_key: raw},
    )
    result = await ImageControl(
        ProfileDatabase(), storage, ImagePolicy(profile_version="pyramid-current")
    ).generation_reference(
        ImageGenerationReferenceRequest(
            actor_id=uuid4(),
            dataset_item_id=owner.id,
            profile_version=selected_profile,
            expires_in=60,
        )
    )

    assert result.pyramid.profile_version == selected_profile
    assert result.pyramid.status == "ready"
    assert result.overview_url.endswith(f"{prefix}/overview.webp?expires=60")


@pytest.mark.asyncio
async def test_explicit_old_profile_rejects_native_current_profile_before_signing():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    current_state, _raw, _prefix = pyramid_projection(current)

    class SubstitutingProfileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            assert request.profile_version == "pyramid-legacy"
            return current_state

    storage = FakeStorage()
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        await ImageControl(
            SubstitutingProfileDatabase(),
            storage,
            ImagePolicy(profile_version="pyramid-current"),
        ).generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(),
                dataset_item_id=owner.id,
                profile_version="pyramid-legacy",
                expires_in=60,
            )
        )

    assert storage.signed_cache == []
    assert storage.read_cache_calls == []


@pytest.mark.asyncio
async def test_control_reads_summaries_from_configured_profile():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    configured_profile = "pyramid-current"
    requested = c.ReadImageSummariesRequest(
        actor_id=uuid4(),
        selectors=[c.ImageSourceSelector(dataset_item_id=owner.id)],
    )
    response = c.ImageSummaries(
        items=[
            c.ImageSummaryItem(
                selector=requested.selectors[0],
                source=current,
                thumbnail_current=current.thumbnail_current,
                pyramid=c.ImagePyramidSummary(
                    profile_version=configured_profile,
                    status="missing",
                    required=False,
                    generation=None,
                    building_generation=None,
                    building_status=None,
                    width=None,
                    height=None,
                    format=None,
                ),
            )
        ]
    )

    class SummaryDatabase:
        async def call(self, operation, request):
            assert operation == "read_image_summaries"
            assert request.profile_version == configured_profile
            return response

    result = await ImageControl(
        SummaryDatabase(),
        FakeStorage(),
        ImagePolicy(profile_version=configured_profile),
    ).call("read_image_summaries", requested)
    assert result.items[0].pyramid.profile_version == configured_profile


@pytest.mark.asyncio
async def test_generation_reference_uses_latest_dimensions_without_auto_dispatch():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    current = c.ImageSource.model_validate(
        {**base.model_dump(), "width": None, "height": None}
    )
    registered = c.ImageSource.model_validate(
        {**current.model_dump(), "width": 100, "height": 100}
    )
    missing, _raw, _prefix = pyramid_projection(registered, status="missing")
    policy = ImagePolicy(
        optional_pixels=1,
        required_pixels=50,
        auto_generate_pyramids=False,
    )
    calls = []
    metadata_ready = False

    class RegisteredDimensionsDatabase:
        async def call(self, operation, request):
            nonlocal metadata_ready
            calls.append(operation)
            if operation in {"read_image_source", "observe_image_source"}:
                return registered if metadata_ready else current
            assert operation == "read_image_generation"
            metadata_ready = True
            return c.ImageGenerationRead(source=registered, pyramid=missing.pyramid)

    result = await ImageControl(
        RegisteredDimensionsDatabase(), FakeStorage(), policy
    ).generation_reference(
        ImageGenerationReferenceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.source.width == 100
    assert result.source.height == 100
    assert result.pyramid.required is True
    assert "prepare_image_media" not in calls


@pytest.mark.asyncio
async def test_verified_current_thumbnail_and_original_are_signed_with_source_recheck():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    key = f"thumbnails/{owner.id}.webp"
    current = source(owner=owner, thumbnail_path=key, thumbnail_current=True)

    class ReadDatabase:
        async def call(self, operation, request):
            assert operation in {"read_image_source", "observe_image_source"}
            return current

    storage = FakeStorage(cache_keys=[key])
    actor = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    result = await ImageControl(ReadDatabase(), storage).source_reference(actor)
    assert result.source == current
    assert result.original_url.endswith("datasets/source/image.png")
    assert result.expires_in == 3600
    assert result.thumbnail_url.endswith(key + "?expires=3600")
    assert storage.signed_source == [(current, 3600)]
    assert storage.signed_cache == [(key, 3600)]
    assert storage.head_source_calls == [current, current]


@pytest.mark.asyncio
async def test_read_only_dataset_reader_verifies_thumbnail_attempt_internally():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    job_id, lease_token = uuid4(), uuid4()
    generation = 3
    key = f"image-assets/{job_id}/g{generation}/a{lease_token}/thumbnail.webp"
    current = source(owner=owner, thumbnail_path=key, thumbnail_current=True)
    policy = ImagePolicy()
    work_status = c.ImageWorkStatus(
        work=c.ImageWorkReference(
            job_id=job_id,
            generation=generation,
            owner=owner,
            source_generation=current.source_generation,
            kind="thumbnail",
            status="ready",
        ),
        spec=c.ThumbnailWorkSpec(
            expected_source_snapshot=current.source_snapshot, policy=policy
        ),
        progress={},
        error_code=None,
        error_detail=None,
        lease_expires_at=None,
    )
    calls = []

    class ReadOnlyDatabase:
        async def call(self, operation, request):
            calls.append((operation, request))
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_internal_image_work":
                assert request.job_id == job_id
                return work_status
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage(cache_keys=[key])
    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    result = await ImageControl(ReadOnlyDatabase(), storage, policy).source_reference(
        request
    )

    assert result.original_url.endswith(current.file_path)
    assert result.thumbnail_url.endswith(key + "?expires=3600")
    assert any(operation == "read_internal_image_work" for operation, _ in calls)
    assert not any(operation == "read_image_work" for operation, _ in calls)
    assert not hasattr(result, "progress")


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_status", ["pending", "running", "failed", "canceled"])
async def test_retained_thumbnail_is_signed_only_for_the_exact_current_source(
    refresh_status,
):
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    job_id = uuid4()
    old_generation = 3
    key = f"image-assets/{job_id}/g{old_generation}/a{uuid4()}/thumbnail.webp"
    current = source(owner=owner, thumbnail_path=key, thumbnail_current=True)
    work_status = retained_thumbnail_work_status(
        current,
        key,
        job_id=job_id,
        generation=old_generation,
        refresh_generation=old_generation + 1,
        status=refresh_status,
    )

    class RetainedDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_internal_image_work"
            assert request.job_id == job_id
            return work_status

    storage = FakeStorage(cache_keys=[key])
    result = await ImageControl(RetainedDatabase(), storage).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.thumbnail_url.endswith(key + "?expires=3600")
    assert storage.head_source_calls == [current, current]
    assert storage.head_cache_calls == [key]
    assert storage.signed_cache == [(key, 3600)]


@pytest.mark.asyncio
async def test_retained_thumbnail_from_a_changed_source_is_never_headed_or_signed():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    job_id = uuid4()
    old_generation = 1
    key = f"image-assets/{job_id}/g{old_generation}/a{uuid4()}/thumbnail.webp"
    original = source(owner=owner, thumbnail_path=key, thumbnail_current=True)
    changed = original.model_copy(update={"source_snapshot": "c" * 64})
    work_status = retained_thumbnail_work_status(
        changed,
        key,
        job_id=job_id,
        generation=old_generation,
        refresh_generation=old_generation + 1,
        status="failed",
        publication_source=original,
    )

    class ChangedSourceDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return changed
            assert operation == "read_internal_image_work"
            return work_status

    storage = FakeStorage(cache_keys=[key])
    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageControl(ChangedSourceDatabase(), storage).source_reference(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
        )
    assert storage.head_cache_calls == []
    assert storage.signed_cache == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "source_generation", "work_kind", "spec_snapshot"),
    [
        (c.ImageOwner(kind="dataset_item", id=uuid4()), 1, "thumbnail", "a" * 64),
        (None, 2, "thumbnail", "a" * 64),
        (None, 1, "pyramid", "a" * 64),
        (None, 1, "thumbnail", "b" * 64),
    ],
)
async def test_read_only_thumbnail_verification_rejects_unrelated_work(
    owner, source_generation, work_kind, spec_snapshot
):
    current_owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    job_id, lease_token = uuid4(), uuid4()
    key = f"image-assets/{job_id}/g3/a{lease_token}/thumbnail.webp"
    current = source(owner=current_owner, thumbnail_path=key, thumbnail_current=True)
    work_status = c.ImageWorkStatus(
        work=c.ImageWorkReference(
            job_id=job_id,
            generation=3,
            owner=owner or current_owner,
            source_generation=source_generation,
            kind=work_kind,
            status="ready",
        ),
        spec=c.ThumbnailWorkSpec(
            expected_source_snapshot=spec_snapshot, policy=ImagePolicy()
        ),
        progress={"private": "must not escape"},
        error_code=None,
        error_detail=None,
        lease_expires_at=None,
    )

    class ReadOnlyDatabase:
        async def call(self, operation, _request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_internal_image_work"
            return work_status

    expected_code = (
        "source_changed"
        if source_generation != 1 or spec_snapshot != "a" * 64
        else "invalid_response"
    )
    with pytest.raises(c.ImageFailure, match=expected_code):
        await ImageControl(
            ReadOnlyDatabase(), FakeStorage(cache_keys=[key])
        ).source_reference(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=current_owner.id)
        )


@pytest.mark.asyncio
async def test_task_pyramid_thumbnail_reference_preserves_legacy_3600s_lifetime():
    owner = c.ImageOwner(kind="task", id=uuid4())
    base = source(owner=owner)
    state, raw, prefix = pyramid_projection(base)
    key = state.pyramid.overview_key
    current = c.ImageSource.model_validate(
        {
            **base.model_dump(),
            "thumbnail_path": key,
            "thumbnail_source_generation": base.source_generation,
        }
    )

    class TaskThumbnailDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            return state

    storage = FakeStorage(
        cache_keys=[key], cache_bytes={state.pyramid.manifest_key: raw}
    )
    result = await ImageControl(TaskThumbnailDatabase(), storage).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), task_id=owner.id)
    )

    assert result.expires_in == 3600
    assert result.thumbnail_url.endswith(f"{prefix}/overview.webp?expires=3600")
    assert storage.signed_source == [(current, 3600)]
    assert storage.signed_cache == [(key, 3600)]


@pytest.mark.asyncio
async def test_internal_source_reference_uses_its_explicit_default_expiry():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)

    class InternalDatabase:
        async def call(self, operation, request):
            assert operation == "resolve_internal_image_source"
            assert request.owner == owner
            return current

    storage = FakeStorage()
    request = InternalImageSourceReferenceRequest(
        source=c.InternalImageSourceRequest(owner=owner)
    )
    result = await ImageControl(InternalDatabase(), storage).internal_source_reference(
        request
    )

    assert result.actor_id is None
    assert result.expires_in == 3600
    assert storage.signed_source == [(current, 3600)]


@pytest.mark.asyncio
async def test_missing_thumbnail_is_invalidated_and_repair_operation_is_replay_stable():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    key = f"thumbnails/{owner.id}.webp"
    current = source(owner=owner, thumbnail_path=key, thumbnail_current=True)
    accepted = []

    class RepairDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "invalidate_image_media":
                assert request.invalidation.kind == "thumbnail"
                assert request.invalidation.source_snapshot == current.source_snapshot
                return c.InvalidationResult(source=current, changed=True)
            assert operation == "prepare_image_media"
            accepted.append(request)
            return accepted_for(request, current)

    storage = FakeStorage()
    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    control = ImageControl(RepairDatabase(), storage)
    first = await control.source_reference(request)
    second = await control.source_reference(request)
    assert first.thumbnail_url is None and second.thumbnail_url is None
    assert len(accepted) == 2
    assert accepted[0].operation_id == accepted[1].operation_id
    assert accepted[0].expected_source_snapshot == current.source_snapshot
    assert accepted[0].force is False
    assert storage.signed_cache == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["permission_denied", "operation_busy"])
async def test_optional_thumbnail_warmup_denial_keeps_authorized_original(
    failure_code,
):
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    calls = []

    class ReaderDatabase:
        async def call(self, operation, _request):
            calls.append(operation)
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "prepare_image_media":
                raise c.ImageFailure(failure_code)
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage()
    result = await ImageControl(ReaderDatabase(), storage).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.original_url.endswith(current.file_path)
    assert result.thumbnail_url is None
    assert calls[-2:] == ["read_image_source", "observe_image_source"]
    assert calls.count("prepare_image_media") == 1
    assert storage.signed_source == [(current, 3600)]
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_optional_thumbnail_invalidation_denial_keeps_authorized_original():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(
        owner=owner,
        thumbnail_path=f"thumbnails/{owner.id}.webp",
        thumbnail_current=True,
    )
    calls = []

    class ReaderDatabase:
        async def call(self, operation, _request):
            calls.append(operation)
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "invalidate_image_media":
                raise c.ImageFailure("permission_denied")
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage()
    result = await ImageControl(ReaderDatabase(), storage).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.original_url.endswith(current.file_path)
    assert result.thumbnail_url is None
    assert calls[-2:] == ["read_image_source", "observe_image_source"]
    assert calls.count("invalidate_image_media") == 1
    assert storage.signed_source == [(current, 3600)]
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_source_reference_still_rechecks_actor_after_best_effort_denial():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    source_reads = 0

    class RevokedActorDatabase:
        async def call(self, operation, _request):
            nonlocal source_reads
            if operation == "read_image_source":
                source_reads += 1
                if source_reads == 3:
                    raise c.ImageFailure("account_not_active")
                return current
            if operation == "observe_image_source":
                return current
            if operation == "prepare_image_media":
                raise c.ImageFailure("permission_denied")
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage()
    with pytest.raises(c.ImageFailure, match="account_not_active"):
        await ImageControl(RevokedActorDatabase(), storage).source_reference(
            c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
        )

    assert storage.signed_source == [(current, 3600)]


@pytest.mark.asyncio
async def test_retry_replays_native_receipt_without_source_io_or_retargeting():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    operation_id = uuid4()
    request = c.RetryImageMediaRequest(
        operation_id=operation_id,
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        purpose="pyramid",
        expected_source_snapshot=None,
        policy=ImagePolicy(),
    )
    spec = c.PyramidWorkSpec(
        expected_source_snapshot=current.source_snapshot, policy=request.policy
    )
    receipt = c.AcceptedImageWork(
        operation_id=operation_id,
        source=current,
        work=c.ImageWorkReference(
            job_id=uuid4(),
            generation=1,
            owner=owner,
            source_generation=current.source_generation,
            kind="pyramid",
            status="ready",
        ),
        spec=spec,
    )

    class ReceiptDatabase:
        async def call(self, operation, forwarded):
            assert operation == "retry_image_media"
            assert forwarded.operation_id == operation_id
            assert forwarded.expected_source_snapshot is None
            return receipt

    class NoSourceIO(FakeStorage):
        async def head_source(self, current):
            pytest.fail("receipt replay attempted object storage")

    control = ImageControl(ReceiptDatabase(), NoSourceIO(), request.policy)
    replay = await control.call("retry_image_media", request)
    assert replay == receipt


@pytest.mark.asyncio
async def test_retry_rejects_native_receipt_with_substituted_policy_before_wakeup():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    request = c.RetryImageMediaRequest(
        operation_id=uuid4(),
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        purpose="pyramid",
        expected_source_snapshot=None,
    )
    substituted_policy = ImagePolicy(max_source_bytes=123456)
    receipt = c.AcceptedImageWork(
        operation_id=request.operation_id,
        source=current,
        work=c.ImageWorkReference(
            job_id=uuid4(),
            generation=1,
            owner=owner,
            source_generation=current.source_generation,
            kind="pyramid",
            status="pending",
        ),
        spec=c.PyramidWorkSpec(
            expected_source_snapshot=current.source_snapshot,
            policy=substituted_policy,
        ),
    )
    wakeups = []

    class SubstitutingDatabase:
        async def call(self, operation, forwarded):
            assert operation == "retry_image_media"
            assert forwarded.operation_id == request.operation_id
            assert forwarded.policy == ImagePolicy()
            return receipt

    async def wakeup(job_id, generation):
        wakeups.append((job_id, generation))

    storage = FakeStorage()
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        await ImageControl(
            SubstitutingDatabase(), storage, wakeup=wakeup
        ).retry_image_media(request)

    assert wakeups == []
    assert storage.head_source_calls == []


@pytest.mark.asyncio
async def test_generation_reference_verifies_actual_geometry_manifest_before_signing():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, prefix = pyramid_projection(current)

    class GenerationDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            return state

    storage = FakeStorage(
        cache_keys=[f"{prefix}/overview.webp"],
        cache_bytes={state.pyramid.manifest_key: raw},
    )
    request = ImageGenerationReferenceRequest(
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        expected_source_snapshot=current.source_snapshot,
        expires_in=123,
    )
    result = await ImageControl(GenerationDatabase(), storage).generation_reference(
        request
    )
    assert result.manifest == state.pyramid.manifest
    assert result.overview_url.endswith(f"{prefix}/overview.webp?expires=123")
    assert result.expires_in == 123
    assert storage.read_cache_calls == [
        (state.pyramid.manifest_key, 1024 * 1024),
        (state.pyramid.manifest_key, 1024 * 1024),
    ]


@pytest.mark.asyncio
async def test_missing_overview_is_invalidated_and_repaired_without_blind_signing():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, prefix = pyramid_projection(current)
    stale = pyramid_projection(current, status="stale")[0]
    admitted = []
    invalidated = []

    class MissingOverviewDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return state if not invalidated else stale
            if operation == "invalidate_image_media":
                invalidated.append(request.invalidation)
                return c.InvalidationResult(source=current, changed=True)
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    storage = FakeStorage(cache_bytes={state.pyramid.manifest_key: raw})
    request = ImageGenerationReferenceRequest(
        actor_id=uuid4(), dataset_item_id=owner.id, expires_in=120
    )
    result = await ImageControl(
        MissingOverviewDatabase(), storage
    ).generation_reference(request)
    assert result.overview_url is None
    assert result.pyramid.status == "stale"
    assert invalidated and invalidated[0].kind == "overview"
    assert invalidated[0].generation == state.pyramid.generation
    assert invalidated[0].profile_version == state.pyramid.profile_version
    assert admitted and admitted[0].purpose == "pyramid"
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_stale_pyramid_thumbnail_pointer_obeys_optional_dispatch_policy():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    stale, _raw, _prefix = pyramid_projection(base, status="stale")
    current = c.ImageSource.model_validate(
        {
            **base.model_dump(),
            "thumbnail_path": stale.pyramid.overview_key,
            "thumbnail_source_generation": base.source_generation,
        }
    )
    calls = []
    admitted = []

    class OptionalPyramidDatabase:
        async def call(self, operation, request):
            calls.append((operation, request))
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return stale
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    policy = ImagePolicy(
        optional_pixels=1,
        required_pixels=1,
        auto_generate_pyramids=False,
    )
    result = await ImageControl(
        OptionalPyramidDatabase(), FakeStorage(), policy
    ).source_reference(request)

    assert result.thumbnail_url is None
    assert admitted == []
    assert not any(
        operation == "prepare_image_media" and request.purpose == "pyramid"
        for operation, request in calls
    )


@pytest.mark.asyncio
async def test_known_large_source_with_auto_generation_disabled_queues_no_thumbnail():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    current = c.ImageSource.model_validate(
        {**base.model_dump(), "width": 100, "height": 100}
    )
    policy = ImagePolicy(
        optional_pixels=10_000,
        required_pixels=20_000,
        auto_generate_pyramids=False,
    )
    calls = []

    class LargeImageDatabase:
        async def call(self, operation, _request):
            calls.append(operation)
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            pytest.fail(f"unexpected operation {operation}")

    storage = FakeStorage()
    result = await ImageControl(LargeImageDatabase(), storage, policy).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.original_url.endswith(current.file_path)
    assert result.thumbnail_url is None
    assert "prepare_image_media" not in calls
    assert storage.signed_source == [(current, 3600)]


@pytest.mark.asyncio
async def test_known_eligible_large_source_queues_pyramid_not_thumbnail():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    current = c.ImageSource.model_validate(
        {**base.model_dump(), "width": 100, "height": 100}
    )
    policy = ImagePolicy(
        optional_pixels=10_000,
        required_pixels=20_000,
        auto_generate_pyramids=True,
    )
    admitted = []

    class LargeImageDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    result = await ImageControl(
        LargeImageDatabase(), FakeStorage(), policy
    ).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert result.original_url.endswith(current.file_path)
    assert result.thumbnail_url is None
    assert len(admitted) == 1
    assert admitted[0].purpose == "pyramid"


@pytest.mark.asyncio
async def test_old_profile_thumbnail_pointer_is_not_signed_without_current_lineage():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    old_state, _old_raw, _old_prefix = pyramid_projection(base)
    old_key = old_state.pyramid.overview_key
    current = c.ImageSource.model_validate(
        {
            **base.model_dump(),
            "thumbnail_path": old_key,
            "thumbnail_source_generation": base.source_generation,
        }
    )
    new_state, _new_raw, _new_prefix = pyramid_projection(current, status="missing")
    configured_profile = "pyramid-current"
    new_state = c.ImageGenerationRead(
        source=current,
        pyramid=new_state.pyramid.model_copy(
            update={"profile_version": configured_profile}
        ),
    )
    admitted = []

    class ProfileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                assert request.profile_version == configured_profile
                return new_state
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    storage = FakeStorage(cache_keys=[old_key])
    result = await ImageControl(
        ProfileDatabase(),
        storage,
        ImagePolicy(profile_version=configured_profile),
    ).source_reference(
        c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    )

    assert old_state.pyramid.status == "ready"
    assert result.thumbnail_url is None
    assert old_key not in storage.head_cache_calls
    assert storage.signed_cache == []
    assert admitted and admitted[0].purpose == "thumbnail"


@pytest.mark.asyncio
async def test_stale_pyramid_thumbnail_with_unknown_dimensions_probes_dimensions_only():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    stale, _raw, _prefix = pyramid_projection(base, status="stale")
    current = c.ImageSource.model_validate(
        {
            **base.model_dump(),
            "width": None,
            "height": None,
            "thumbnail_path": stale.pyramid.overview_key,
            "thumbnail_source_generation": base.source_generation,
        }
    )
    admitted = []

    class UnknownDimensionsDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return stale
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    result = await ImageControl(
        UnknownDimensionsDatabase(), FakeStorage()
    ).source_reference(request)

    assert result.thumbnail_url is None
    assert len(admitted) == 1
    assert admitted[0].purpose == "dimensions"


@pytest.mark.asyncio
async def test_unknown_dimensions_probe_before_small_thumbnail_generation():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    base = source(owner=owner)
    current = c.ImageSource.model_validate(
        {**base.model_dump(), "width": None, "height": None}
    )
    admitted = []

    class UnknownDimensionsDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    request = c.ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=owner.id)
    result = await ImageControl(
        UnknownDimensionsDatabase(), FakeStorage()
    ).source_reference(request)

    assert result.thumbnail_url is None
    assert len(admitted) == 1
    assert admitted[0].purpose == "dimensions"


@pytest.mark.asyncio
async def test_foreign_manifest_key_is_invalidated_instead_of_signed():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, _prefix = pyramid_projection(current)
    foreign_asset = uuid4()
    foreign_prefix = f"image-pyramids/{foreign_asset}/g2/a{uuid4()}"
    bad_pyramid = state.pyramid.model_copy(
        update={"manifest_key": f"{foreign_prefix}/manifest.json"}
    )
    bad_state = c.ImageGenerationRead(source=current, pyramid=bad_pyramid)
    stale = pyramid_projection(current, status="stale")[0]
    invalidated = []

    class ForeignManifestDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return stale if invalidated else bad_state
            if operation == "invalidate_image_media":
                invalidated.append(request.invalidation)
                return c.InvalidationResult(source=current, changed=True)
            assert operation == "prepare_image_media"
            return accepted_for(request, current)

    storage = FakeStorage(
        cache_keys=[f"{state.pyramid.overview_key}"],
        cache_bytes={state.pyramid.manifest_key: raw},
    )
    result = await ImageControl(
        ForeignManifestDatabase(), storage
    ).generation_reference(
        ImageGenerationReferenceRequest(
            actor_id=uuid4(), dataset_item_id=owner.id, expires_in=60
        )
    )
    assert result.overview_url is None
    assert invalidated and invalidated[0].kind == "manifest"
    assert storage.signed_cache == []


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_bytes", [None, b"not-json"])
async def test_asset_reference_invalidates_and_repairs_missing_or_corrupt_manifest(
    manifest_bytes,
):
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, _raw, _prefix = pyramid_projection(current)
    invalidated = []
    admitted = []

    class BrokenManifestDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            if operation == "read_image_generation":
                return state
            if operation == "invalidate_image_media":
                invalidated.append(request.invalidation)
                return c.InvalidationResult(source=current, changed=True)
            assert operation == "prepare_image_media"
            admitted.append(request)
            return accepted_for(request, current)

    cache_bytes = (
        {} if manifest_bytes is None else {state.pyramid.manifest_key: manifest_bytes}
    )
    storage = FakeStorage(cache_bytes=cache_bytes)
    request = ImageAssetReferencesRequest(
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        items=[{"kind": "overview", "generation": state.pyramid.generation}],
    )

    with pytest.raises(c.ImageFailure, match="object_missing"):
        await ImageControl(BrokenManifestDatabase(), storage).asset_references(request)

    assert invalidated and invalidated[0].kind == "manifest"
    assert invalidated[0].generation == state.pyramid.generation
    assert admitted and admitted[0].purpose == "pyramid"
    assert storage.signed_cache == []
    assert storage.head_cache_calls == []


@pytest.mark.asyncio
async def test_asset_reference_constructs_tile_key_from_manifest_attempt_prefix():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, prefix = pyramid_projection(current)
    selected_profile = "pyramid-legacy"
    state = c.ImageGenerationRead(
        source=current,
        pyramid=state.pyramid.model_copy(update={"profile_version": selected_profile}),
    )
    key = f"{prefix}/tiles/0/0/0.webp"

    class TileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            assert request.profile_version == selected_profile
            return state

    storage = FakeStorage(
        cache_keys=[key], cache_bytes={state.pyramid.manifest_key: raw}
    )
    request = ImageAssetReferencesRequest(
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        expected_source_snapshot=current.source_snapshot,
        profile_version=selected_profile,
        items=[{"kind": "tile", "generation": 2, "level": 0, "x": 0, "y": 0}],
    )
    result = await ImageControl(TileDatabase(), storage).asset_references(request)
    assert len(result.items) == 1
    assert result.profile_version == selected_profile
    assert storage.head_cache_calls == [key]
    assert storage.signed_cache == [(key, 900)]
    assert result.items[0].url.endswith(key + "?expires=900")


@pytest.mark.asyncio
async def test_out_of_manifest_tile_coordinate_never_reaches_storage_signer():
    owner = c.ImageOwner(kind="dataset_item", id=uuid4())
    current = source(owner=owner)
    state, raw, _prefix = pyramid_projection(current)

    class TileDatabase:
        async def call(self, operation, request):
            if operation in {"read_image_source", "observe_image_source"}:
                return current
            assert operation == "read_image_generation"
            return state

    storage = FakeStorage(cache_bytes={state.pyramid.manifest_key: raw})
    request = ImageAssetReferencesRequest(
        actor_id=uuid4(),
        dataset_item_id=owner.id,
        items=[{"kind": "tile", "generation": 2, "level": 2, "x": 0, "y": 0}],
    )
    with pytest.raises(c.ImageFailure, match="invalid_coordinate"):
        await ImageControl(TileDatabase(), storage).asset_references(request)
    assert storage.signed_cache == []
    assert storage.head_cache_calls == []
