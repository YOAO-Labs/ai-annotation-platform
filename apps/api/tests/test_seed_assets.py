from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

import httpx
import pytest

from scripts.seed_assets import (
    SeedAsset,
    SeedAssetError,
    ensure_asset,
    ensure_profile,
    load_manifest,
    select_profile,
)


def _asset(
    payload: bytes,
    *,
    archive: str = "none",
    root: str = ".",
    required: tuple[str, ...] = ("fixture.bin",),
    filename: str | None = "fixture.bin",
    max_unpacked_bytes: int = 1024,
) -> SeedAsset:
    return SeedAsset(
        id="fixture",
        profiles=("screenshots",),
        urls=("https://assets.example/fixture",),
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        max_bytes=max(len(payload), 1),
        max_unpacked_bytes=max_unpacked_bytes,
        archive=archive,
        root=PurePosixPath(root),
        required_files=tuple(PurePosixPath(item) for item in required),
        source="https://assets.example/source",
        artifact_spdx="MIT",
        media_status="project-owned",
        notice_url="https://assets.example/license",
        attribution="Example",
        public_docs_status="approved",
        public_docs_notes="Test fixture",
        filename=filename,
    )


def _client(payload: bytes, calls: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _tar(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/outside"
            archive.addfile(info)
    return output.getvalue()


def test_repository_manifest_records_public_docs_clearance():
    assets = load_manifest()

    assert {asset.id for asset in assets} == {
        "auckland-traffic-1",
        "auckland-traffic-2",
        "coco8",
        "coco8-seg",
        "nuscenes-demo-lidar",
        "nuscenes-demo-cam-front",
        "nuscenes-demo-cam-front-left",
        "nuscenes-demo-cam-front-right",
        "nuscenes-demo-cam-back",
        "nuscenes-demo-cam-back-left",
        "nuscenes-demo-cam-back-right",
        "pcl-pairwise-capture-1",
        "pcl-pairwise-capture-2",
        "pcl-pairwise-capture-3",
        "pcl-pairwise-capture-4",
        "tracking-video",
        "rapidocr-image",
        "street-traffic-video",
        "sustechpoints-example",
    }
    statuses = {asset.id: asset.public_docs_status for asset in assets}
    assert statuses == {
        "auckland-traffic-1": "approved",
        "auckland-traffic-2": "approved",
        "coco8": "blocked",
        "coco8-seg": "blocked",
        "nuscenes-demo-lidar": "approved_with_attribution",
        "nuscenes-demo-cam-front": "approved_with_attribution",
        "nuscenes-demo-cam-front-left": "approved_with_attribution",
        "nuscenes-demo-cam-front-right": "approved_with_attribution",
        "nuscenes-demo-cam-back": "approved_with_attribution",
        "nuscenes-demo-cam-back-left": "approved_with_attribution",
        "nuscenes-demo-cam-back-right": "approved_with_attribution",
        "pcl-pairwise-capture-1": "approved_with_attribution",
        "pcl-pairwise-capture-2": "approved_with_attribution",
        "pcl-pairwise-capture-3": "approved_with_attribution",
        "pcl-pairwise-capture-4": "approved_with_attribution",
        "tracking-video": "review_required",
        "rapidocr-image": "approved_with_attribution",
        "street-traffic-video": "approved_with_attribution",
        "sustechpoints-example": "review_required",
    }
    assert {asset.id for asset in assets if "screenshots" in asset.profiles} == {
        "auckland-traffic-1",
        "auckland-traffic-2",
        "nuscenes-demo-lidar",
        "nuscenes-demo-cam-front",
        "nuscenes-demo-cam-front-left",
        "nuscenes-demo-cam-front-right",
        "nuscenes-demo-cam-back",
        "nuscenes-demo-cam-back-left",
        "nuscenes-demo-cam-back-right",
        "rapidocr-image",
        "street-traffic-video",
    }


def test_screenshot_profile_fails_closed_when_required_media_is_not_cleared():
    with pytest.raises(SeedAssetError, match="missing required or approved assets"):
        select_profile(
            "screenshots",
            required_ids={"coco8", "tracking-video", "rapidocr-image"},
        )


def test_screenshot_profile_rejects_uncleared_manifest_entry():
    asset = replace(
        _asset(b"fixture"),
        profiles=("screenshots",),
        public_docs_status="review_required",
    )

    with pytest.raises(SeedAssetError, match="not cleared for public docs"):
        select_profile("screenshots", assets=(asset,))


def test_raw_asset_download_is_verified_cached_and_works_offline(tmp_path: Path):
    payload = b"verified seed asset"
    asset = _asset(payload)
    calls: list[str] = []
    with _client(payload, calls) as client:
        first = ensure_asset(asset, cache_dir=tmp_path, client=client)
        second = ensure_asset(asset, cache_dir=tmp_path, offline=True, client=client)

    assert first.root.joinpath("fixture.bin").read_bytes() == payload
    assert second.source == "cache"
    assert calls == ["https://assets.example/fixture"]


def test_corrupt_cache_is_rejected_in_offline_mode(tmp_path: Path):
    payload = b"verified seed asset"
    asset = _asset(payload)
    with _client(payload, []) as client:
        resolved = ensure_asset(asset, cache_dir=tmp_path, client=client)
    resolved.root.parent.joinpath("source.bin").write_bytes(b"tampered")

    with pytest.raises(SeedAssetError, match="offline mode"):
        ensure_asset(asset, cache_dir=tmp_path, offline=True)


def test_hash_mismatch_cleans_staging_directory(tmp_path: Path):
    asset = _asset(b"expected")
    with _client(b"different", []) as client:
        with pytest.raises(SeedAssetError, match="all download URLs failed"):
            ensure_asset(asset, cache_dir=tmp_path, client=client)

    asset_parent = tmp_path / asset.id
    assert not asset_parent.exists() or list(asset_parent.iterdir()) == []


def test_zip_path_traversal_is_rejected(tmp_path: Path):
    payload = _zip({"../outside.txt": b"nope", "fixture.bin": b"ok"})
    asset = _asset(payload, archive="zip", filename=None)

    with _client(payload, []) as client:
        with pytest.raises(SeedAssetError, match="unsafe archive path"):
            ensure_asset(asset, cache_dir=tmp_path, client=client)
    assert not (tmp_path / "outside.txt").exists()


def test_tar_symlink_is_rejected(tmp_path: Path):
    payload = _tar({"fixture.bin": b"ok"}, symlink="outside-link")
    asset = _asset(payload, archive="tar.gz", filename=None)

    with _client(payload, []) as client:
        with pytest.raises(SeedAssetError, match="link/device"):
            ensure_asset(asset, cache_dir=tmp_path, client=client)


def test_unpacked_size_limit_is_enforced(tmp_path: Path):
    payload = _zip({"fixture.bin": b"too large"})
    asset = _asset(
        payload,
        archive="zip",
        filename=None,
        max_unpacked_bytes=3,
    )

    with _client(payload, []) as client:
        with pytest.raises(SeedAssetError, match="unpacked content exceeds"):
            ensure_asset(asset, cache_dir=tmp_path, client=client)


def test_local_override_and_profile_resolution(tmp_path: Path):
    payload = b"local fixture"
    asset = _asset(payload)
    fixture_root = tmp_path / "assets" / asset.id
    fixture_root.mkdir(parents=True)
    fixture_root.joinpath("fixture.bin").write_bytes(payload)

    resolved = ensure_profile(
        "screenshots",
        asset_dir=tmp_path / "assets",
        assets=(asset,),
    )

    assert resolved[asset.id].root == fixture_root
    assert resolved[asset.id].source == "local-override"


def test_manifest_rejects_unsafe_required_path(tmp_path: Path):
    manifest = tmp_path / "assets.toml"
    manifest.write_text(
        """
schema_version = 1
[[assets]]
id = "fixture"
profiles = ["screenshots"]
urls = ["https://assets.example/fixture"]
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
size_bytes = 1
max_bytes = 1
max_unpacked_bytes = 1
archive = "none"
filename = "fixture.bin"
root = "."
required_files = ["../outside"]
source = "https://assets.example/source"
[assets.license]
artifact_spdx = "MIT"
media_status = "project-owned"
notice_url = "https://assets.example/license"
attribution = "Example"
public_docs_status = "approved"
public_docs_notes = "Test fixture"
""",
        encoding="utf-8",
    )

    with pytest.raises(SeedAssetError, match="must stay inside"):
        load_manifest(manifest)
