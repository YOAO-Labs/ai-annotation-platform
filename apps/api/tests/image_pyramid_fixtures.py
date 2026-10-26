"""Canonical pyramid metadata with a deliberately fake physical transport.

No helper infers an original or output from a path. Business tests may opt into
the documented same-session read seam; authority tests keep real owned sessions.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import uuid

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
    ImagePyramidOutput,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import image_pyramid as pyramids
from app.services import image_pyramid_objects as objects
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset
from app.services.storage import storage_service
from tests.factory import build_tool_bindings

SOURCE = b"registered pyramid original" * 40


async def make_image_task(db, owner_id, *, width=8193, height=6145):
    project = Project(
        display_id=f"P-PYR-{uuid.uuid4().hex[:8]}",
        name="Pyramid project",
        type_key="image-segmentation",
        type_label="Image",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["defect"], unit="region"),
    )
    dataset = Dataset(
        display_id=f"D-PYR-{uuid.uuid4().hex[:8]}",
        name=f"pyramid-{uuid.uuid4().hex[:8]}",
        data_type="image",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="large.png",
        file_path=f"pyramid/{uuid.uuid4()}.png",
        file_type="image",
        file_size=len(SOURCE),
        width=width,
        height=height,
    )
    db.add(item)
    await db.flush()
    binding = await ensure_legacy_bindings(db)
    await register_item_asset(
        db,
        item,
        sha256=hashlib.sha256(SOURCE).hexdigest(),
        size_bytes=len(SOURCE),
        etag='"registered-original"',
        binding=binding,
    )
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-PYR-{uuid.uuid4().hex[:8]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return task, item


async def ready_generation(db, item, *, pinned=True, asset=None, generation=None):
    owner = await pyramids.owner_for_dataset_item(db, item)
    if asset is None:
        asset = ImagePyramidAsset(
            dataset_item_id=item.id,
            profile_version="pyramid-v1",
            active_generation=1,
        )
        db.add(asset)
        await db.flush()
    levels = pyramids.pyramid_levels(item.width, item.height)
    overview = next(
        level for level in levels if level["width"] <= 512 and level["height"] <= 512
    )
    output_id, token = uuid.uuid4(), uuid.uuid4()
    ref = await physical.allocate(db, output_id)
    physical_owner = physical.resolve_ref(ref)
    overview_bytes, tile_bytes = b"verified-overview", b"verified-tile"
    manifest = pyramids.build_manifest(
        generation=1,
        source_fingerprint="sha256:" + owner.source.sha256,
        width=item.width,
        height=item.height,
        overview_width=overview["width"],
        overview_height=overview["height"],
        overview_digest=pyramids.sha256_digest(overview_bytes),
    )
    manifest_bytes = pyramids.canonical_manifest_bytes(manifest)
    content = {
        physical_owner.key(f"tiles/{level['level']}/{x}/{y}.webp"): tile_bytes
        for level in levels
        for y in range(level["rows"])
        for x in range(level["columns"])
    }
    tile_count = len(content)
    content[physical_owner.key("manifest.json")] = manifest_bytes
    content[physical_owner.key("overview.webp")] = overview_bytes
    inventory = {
        "schema": 1,
        "objects": {
            key: {"sha256": hashlib.sha256(value).hexdigest(), "size_bytes": len(value)}
            for key, value in content.items()
        },
    }
    values = dict(
        asset_id=asset.id,
        generation=1,
        source_identity=pyramids.source_identity(owner)[0],
        original_ref=originals.receipt(owner.source),
        auth_ref=originals.system_authorization("dataset_item", item.id, owner.source),
        source_fingerprint="sha256:" + owner.source.sha256,
        status="ready",
        width=item.width,
        height=item.height,
        max_level=len(levels) - 1,
        format="webp",
        tile_size=512,
        overlap=1,
        normalization_version=manifest["normalizationVersion"],
        manifest_key=physical_owner.key("manifest.json"),
        manifest_digest=pyramids.sha256_digest(manifest_bytes),
        overview_key=physical_owner.key("overview.webp"),
        overview_width=overview["width"],
        overview_height=overview["height"],
        overview_digest=pyramids.sha256_digest(overview_bytes),
        tile_count=tile_count,
        retained_bytes=sum(map(len, content.values())),
    )
    if generation is None:
        generation = ImagePyramidGeneration(**values)
    else:
        for name, value in values.items():
            setattr(generation, name, value)
    output = None
    if pinned:
        output = ImagePyramidOutput(
            id=output_id,
            asset_id=asset.id,
            generation=1,
            lease_token=token,
            storage_ref=ref,
            inventory=None,
            state="building",
        )
        db.add(output)
        await db.flush()
        # Exercise the same immutable-ledger state transition as publication.
        # Inserting ready directly is forbidden by the database owner guard.
        output.inventory = inventory
        output.state = "ready"
        await db.flush()
        generation.output_id = output_id
        objects.validate_inventory(output, generation, inventory)
    db.add(generation)
    asset.active_generation = generation.generation
    asset.building_generation = None
    await db.flush()
    return asset, generation, output, manifest


class PhysicalTransport:
    """Fake only remote bytes/signatures; retain real receipt/authority checks."""

    def __init__(self, monkeypatch):
        from app.services import media_cache_urls

        self.manifests, self.missing, self.verified, self.signed = {}, set(), [], []
        monkeypatch.setattr(physical, "read_manifest", self.read_manifest)
        monkeypatch.setattr(physical, "verify_object", self.verify_object)
        monkeypatch.setattr(storage_service, "generate_download_url", self.download_url)
        sign = media_cache_urls.sign

        def capability(kind, output_id, key, storage_ref, receipt, expires):
            self.signed.append((key, {"bucket": storage_ref["bucket"]}))
            return sign(kind, output_id, key, storage_ref, receipt, expires)

        monkeypatch.setattr(media_cache_urls, "sign", capability)

    def register(self, output, manifest):
        self.manifests[output.id] = deepcopy(manifest)

    def read_manifest(self, _storage, output, digest):
        value = self.manifests[output.id]
        assert digest == pyramids.sha256_digest(
            pyramids.canonical_manifest_bytes(value)
        )
        return deepcopy(value)

    def verify_object(self, _storage, output, key):
        physical.resolve(output).contains(key)
        assert key in output.inventory["objects"]
        self.verified.append(key)
        if key in self.missing:
            raise physical.ImagePyramidStorageError("missing")
        return b""

    def download_url(self, key, **options):
        self.signed.append((key, options))
        assert options["bucket"]
        return f"https://storage.invalid/{options['bucket']}/{key}"


def same_session_reading(monkeypatch):
    """Business-only seam for SAVEPOINT fixtures, never a cross-session proof.

    Canonical input and complete output inventories remain real. Durable Gate
    admission and independent-session revocation are exercised by the separate
    committed HTTP fixture, rather than committing the standard caller fixture.
    """

    @asynccontextmanager
    async def reading(db, owner, generation, *, authority=None, keys=None):
        await originals.check(db, authority, lock=True)
        output = await db.get(ImagePyramidOutput, generation.output_id)
        assert output.asset_id == generation.asset_id
        assert output.generation == generation.generation and output.state == "ready"
        assert generation.original_ref == originals.receipt(owner.source)
        objects.validate_inventory(output, generation, output.inventory)
        yield objects.snapshot(output)
        await originals.check(db, authority, lock=True)

    monkeypatch.setattr(objects, "reading", reading)

    @asynccontextmanager
    async def transport(db, output):
        yield storage_service

    monkeypatch.setattr(objects, "transport", transport)
