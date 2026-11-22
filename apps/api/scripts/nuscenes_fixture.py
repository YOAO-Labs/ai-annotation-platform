"""Download and prepare the official nuScenes mini fixture outside the repository."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import httpx


NUSCENES_MINI_URL = "https://www.nuscenes.org/data/v1.0-mini.tgz"
NUSCENES_MINI_SIZE = 4_167_696_325
NUSCENES_MINI_SHA256 = (
    "943037abbb3b26b3070dc76504a43eb440503b00baf9ac2f1538d9c03fc9298f"
)
NUSCENES_MINI_ETAG = "0e429041111a03d43ec0419e1eb02d5f-497"
NUSCENES_VERSION = "v1.0-mini"
NUSCENES_REQUIRED_SCENES = ("scene-0061",)
DOWNLOAD_PARTS = 8
MAX_EXTRACTED_BYTES = 6 * 1024**3
DOWNLOAD_USER_AGENT = (
    "ai-annotation-platform-seed/1.0 "
    "(https://github.com/YOAO-Labs/ai-annotation-platform)"
)


class NuScenesFixtureError(RuntimeError):
    """The official archive could not be downloaded, verified, or prepared."""


def default_nuscenes_cache_dir() -> Path:
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "ai-annotation-platform" / "nuscenes-mini"


def _load_table(root: Path, name: str) -> list[dict]:
    path = root / NUSCENES_VERSION / f"{name}.json"
    try:
        with path.open(encoding="utf-8") as source:
            rows = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise NuScenesFixtureError(f"nuScenes metadata is invalid: {path}") from exc
    if not isinstance(rows, list):
        raise NuScenesFixtureError(f"nuScenes metadata is not a list: {path}")
    return rows


def validate_nuscenes_mini(
    root: Path, *, required_scenes: tuple[str, ...] = NUSCENES_REQUIRED_SCENES
) -> None:
    """Validate metadata and every key-frame sensor file needed by the seed scenes."""
    if not root.is_dir():
        raise NuScenesFixtureError(f"nuScenes mini root is missing: {root}")

    scenes = _load_table(root, "scene")
    samples = _load_table(root, "sample")
    sample_data = _load_table(root, "sample_data")
    calibrated_sensors = _load_table(root, "calibrated_sensor")
    sensors = _load_table(root, "sensor")
    for row in _load_table(root, "map"):
        filename = row.get("filename")
        if not isinstance(filename, str) or not (root / filename).is_file():
            raise NuScenesFixtureError(f"nuScenes map is missing: {filename}")

    scene_by_name = {row.get("name"): row for row in scenes}
    sample_by_token = {row.get("token"): row for row in samples}
    calibrated_by_token = {row.get("token"): row for row in calibrated_sensors}
    sensor_by_token = {row.get("token"): row for row in sensors}

    sample_tokens: set[str] = set()
    for scene_name in required_scenes:
        scene = scene_by_name.get(scene_name)
        if scene is None:
            raise NuScenesFixtureError(f"nuScenes mini scene is missing: {scene_name}")
        token = scene.get("first_sample_token")
        while token:
            sample = sample_by_token.get(token)
            if sample is None:
                raise NuScenesFixtureError(
                    f"nuScenes mini sample chain is broken at: {token}"
                )
            sample_tokens.add(token)
            token = sample.get("next") or ""

    missing: list[str] = []
    lidar_files = 0
    camera_files = 0
    for row in sample_data:
        if row.get("sample_token") not in sample_tokens or not row.get("is_key_frame"):
            continue
        calibrated = calibrated_by_token.get(row.get("calibrated_sensor_token"))
        sensor = (
            sensor_by_token.get(calibrated.get("sensor_token")) if calibrated else None
        )
        modality = sensor.get("modality") if sensor else None
        if modality not in {"lidar", "camera"}:
            continue
        filename = row.get("filename")
        if not isinstance(filename, str) or not (root / filename).is_file():
            missing.append(str(filename))
        elif modality == "lidar":
            lidar_files += 1
        else:
            camera_files += 1

    if missing:
        preview = ", ".join(missing[:3])
        raise NuScenesFixtureError(
            f"nuScenes mini key-frame files are incomplete ({len(missing)} missing): {preview}"
        )
    if lidar_files != len(sample_tokens) or camera_files < len(sample_tokens):
        raise NuScenesFixtureError(
            "nuScenes mini does not contain the expected lidar/camera key frames"
        )


def _range_bounds(total_size: int, parts: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        (index * total_size // parts, (index + 1) * total_size // parts - 1)
        for index in range(parts)
    )


def _download_range(
    *,
    url: str,
    target: Path,
    start: int,
    end: int,
    total_size: int,
    expected_etag: str | None,
) -> None:
    expected_size = end - start + 1
    received = target.stat().st_size if target.is_file() else 0
    if received > expected_size:
        raise NuScenesFixtureError(f"download part is oversized: {target}")
    if received == expected_size:
        return

    range_start = start + received
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
    headers = {
        "User-Agent": DOWNLOAD_USER_AGENT,
        "Range": f"bytes={range_start}-{end}",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 206:
                    raise NuScenesFixtureError(
                        f"range request returned HTTP {response.status_code}"
                    )
                expected_range = f"bytes {range_start}-{end}/{total_size}"
                if response.headers.get("content-range") != expected_range:
                    raise NuScenesFixtureError(
                        "range response does not match the requested bytes"
                    )
                response_etag = response.headers.get("etag", "").strip('"')
                if expected_etag and response_etag != expected_etag:
                    raise NuScenesFixtureError(
                        f"archive ETag changed: {response_etag or 'missing'}"
                    )
                with target.open("ab") as output:
                    for chunk in response.iter_bytes(chunk_size=4 * 1024 * 1024):
                        output.write(chunk)
    except httpx.HTTPError as exc:
        raise NuScenesFixtureError(f"nuScenes mini download failed: {exc}") from exc

    if target.stat().st_size != expected_size:
        raise NuScenesFixtureError(f"download part is incomplete: {target}")


def _verify_archive(archive: Path, *, expected_size: int, expected_sha256: str) -> None:
    if not archive.is_file() or archive.stat().st_size != expected_size:
        raise NuScenesFixtureError(f"nuScenes mini archive size mismatch: {archive}")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise NuScenesFixtureError("nuScenes mini archive SHA-256 mismatch")


def download_nuscenes_mini(
    cache_dir: Path,
    *,
    url: str = NUSCENES_MINI_URL,
    expected_size: int = NUSCENES_MINI_SIZE,
    expected_sha256: str = NUSCENES_MINI_SHA256,
    expected_etag: str | None = NUSCENES_MINI_ETAG,
    parts: int = DOWNLOAD_PARTS,
) -> Path:
    """Download the fixed archive with bounded parallel ranges and resume support."""
    if not expected_sha256:
        raise NuScenesFixtureError("nuScenes mini archive SHA-256 is not configured")
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / "v1.0-mini.tgz"
    if archive.exists():
        _verify_archive(
            archive,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        return archive

    bounds = _range_bounds(expected_size, parts)
    legacy_part = cache_dir / "v1.0-mini.tgz.part"
    first_part = cache_dir / "v1.0-mini.tgz.part.00"
    if legacy_part.is_file() and not first_part.exists():
        if legacy_part.stat().st_size <= bounds[0][1] - bounds[0][0] + 1:
            legacy_part.replace(first_part)

    with ThreadPoolExecutor(max_workers=parts) as executor:
        futures = {
            executor.submit(
                _download_range,
                url=url,
                target=cache_dir / f"v1.0-mini.tgz.part.{index:02d}",
                start=start,
                end=end,
                total_size=expected_size,
                expected_etag=expected_etag,
            ): index
            for index, (start, end) in enumerate(bounds)
        }
        for future in as_completed(futures):
            index = futures[future]
            future.result()
            print(f"  fetch nuScenes mini part {index + 1}/{parts}")

    merge_path = cache_dir / ".v1.0-mini.tgz.merging"
    digest = hashlib.sha256()
    written = 0
    with merge_path.open("wb") as output:
        for index in range(parts):
            part = cache_dir / f"v1.0-mini.tgz.part.{index:02d}"
            with part.open("rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    if written != expected_size or digest.hexdigest() != expected_sha256:
        merge_path.unlink(missing_ok=True)
        raise NuScenesFixtureError("merged nuScenes mini archive failed verification")
    merge_path.replace(archive)
    for index in range(parts):
        (cache_dir / f"v1.0-mini.tgz.part.{index:02d}").unlink(missing_ok=True)
    return archive


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise NuScenesFixtureError(f"unsafe nuScenes archive path: {name}")
    return path


def extract_nuscenes_mini(archive: Path, root: Path) -> None:
    """Extract metadata, maps and key-frame samples; skip unused sweeps."""
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".content-", dir=root.parent))
    extracted = 0
    try:
        with tarfile.open(archive, mode="r:gz") as source_archive:
            for member in source_archive:
                path = _safe_member_path(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise NuScenesFixtureError(
                        f"nuScenes archive link/device is not allowed: {path}"
                    )
                if path.parts[0] not in {NUSCENES_VERSION, "samples", "maps"}:
                    continue
                mode = member.mode
                if member.isdir():
                    target = staging.joinpath(*path.parts)
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise NuScenesFixtureError(
                        f"unsupported nuScenes archive member: {path}"
                    )
                extracted += member.size
                if extracted > MAX_EXTRACTED_BYTES:
                    raise NuScenesFixtureError(
                        "nuScenes mini extracted content exceeds the safety limit"
                    )
                target = staging.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = source_archive.extractfile(member)
                if source is None:
                    raise NuScenesFixtureError(f"cannot read archive member: {path}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=4 * 1024 * 1024)
        validate_nuscenes_mini(staging)
        if root.exists():
            raise NuScenesFixtureError(
                f"invalid nuScenes mini cache already exists; remove it first: {root}"
            )
        staging.replace(root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def ensure_nuscenes_mini(
    *, cache_dir: Path | None = None, offline: bool = False
) -> Path:
    """Return a validated local nuScenes mini root, downloading it when necessary."""
    cache = cache_dir or default_nuscenes_cache_dir()
    root = cache / "content"
    try:
        validate_nuscenes_mini(root)
        return root
    except NuScenesFixtureError:
        if root.exists():
            raise

    archive = cache / "v1.0-mini.tgz"
    if archive.exists():
        _verify_archive(
            archive,
            expected_size=NUSCENES_MINI_SIZE,
            expected_sha256=NUSCENES_MINI_SHA256,
        )
    elif offline:
        raise NuScenesFixtureError(
            f"nuScenes mini is not cached for --offline: {cache}"
        )
    else:
        archive = download_nuscenes_mini(cache)
    extract_nuscenes_mini(archive, root)
    return root


if __name__ == "__main__":
    prepared = ensure_nuscenes_mini()
    print(f"nuScenes mini ready: {prepared}")
