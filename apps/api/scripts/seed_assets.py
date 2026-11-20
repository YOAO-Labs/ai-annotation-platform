"""Fetch versioned development seed assets into a verified local cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

import httpx


MANIFEST_PATH = Path(__file__).with_name("seed-assets.toml")
SUPPORTED_ARCHIVES = {"none", "zip", "tar.gz"}
PUBLIC_DOCS_STATUSES = {
    "approved",
    "approved_with_attribution",
    "review_required",
    "blocked",
}
DOWNLOAD_USER_AGENT = (
    "ai-annotation-platform-seed/1.0 "
    "(https://github.com/yyq19990828/ai-annotation-platform)"
)


class SeedAssetError(RuntimeError):
    """The asset manifest, download, or extracted content is invalid."""


@dataclass(frozen=True)
class SeedAsset:
    id: str
    profiles: tuple[str, ...]
    urls: tuple[str, ...]
    sha256: str
    size_bytes: int
    max_bytes: int
    max_unpacked_bytes: int
    archive: str
    root: PurePosixPath
    required_files: tuple[PurePosixPath, ...]
    source: str
    artifact_spdx: str
    media_status: str
    notice_url: str
    attribution: str
    public_docs_status: str
    public_docs_notes: str
    filename: str | None = None


@dataclass(frozen=True)
class ResolvedSeedAsset:
    asset: SeedAsset
    root: Path
    source: str


def default_cache_dir() -> Path:
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "ai-annotation-platform" / "seed-assets"


def _relative_path(value: object, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SeedAssetError(f"{field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SeedAssetError(f"{field} must stay inside the asset root: {value}")
    return path


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SeedAssetError(f"{field} must be a positive integer")
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[SeedAsset, ...]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SeedAssetError(f"cannot load seed asset manifest {path}: {exc}") from exc

    if raw.get("schema_version") != 1:
        raise SeedAssetError("unsupported seed asset manifest schema_version")
    rows = raw.get("assets")
    if not isinstance(rows, list) or not rows:
        raise SeedAssetError("seed asset manifest must contain [[assets]] entries")

    seen: set[str] = set()
    assets: list[SeedAsset] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SeedAssetError(f"assets[{index}] must be a table")
        asset_id = row.get("id")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or not all(ch.islower() or ch.isdigit() or ch == "-" for ch in asset_id)
        ):
            raise SeedAssetError(f"assets[{index}].id must use lowercase kebab-case")
        if asset_id in seen:
            raise SeedAssetError(f"duplicate seed asset id: {asset_id}")
        seen.add(asset_id)

        profiles = row.get("profiles")
        urls = row.get("urls")
        required_files = row.get("required_files")
        if (
            not isinstance(profiles, list)
            or not profiles
            or not all(isinstance(value, str) and value for value in profiles)
        ):
            raise SeedAssetError(f"{asset_id}.profiles must be a non-empty string list")
        if (
            not isinstance(urls, list)
            or not urls
            or not all(
                isinstance(value, str) and value.startswith("https://")
                for value in urls
            )
        ):
            raise SeedAssetError(f"{asset_id}.urls must contain HTTPS URLs")
        if not isinstance(required_files, list) or not required_files:
            raise SeedAssetError(f"{asset_id}.required_files must not be empty")

        digest = row.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SeedAssetError(f"{asset_id}.sha256 must be a SHA-256 hex digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise SeedAssetError(f"{asset_id}.sha256 must be hexadecimal") from exc

        archive = row.get("archive")
        if archive not in SUPPORTED_ARCHIVES:
            raise SeedAssetError(f"{asset_id}.archive is unsupported: {archive}")
        filename = row.get("filename")
        if archive == "none":
            _relative_path(filename, field=f"{asset_id}.filename")
        elif filename is not None:
            raise SeedAssetError(f"{asset_id}.filename is only valid for raw assets")

        source = row.get("source")
        license_info = row.get("license")
        if not isinstance(source, str) or not source.startswith("https://"):
            raise SeedAssetError(f"{asset_id}.source must be an HTTPS URL")
        if not isinstance(license_info, dict):
            raise SeedAssetError(f"{asset_id}.license must be a table")
        license_fields = (
            "artifact_spdx",
            "media_status",
            "notice_url",
            "attribution",
            "public_docs_status",
            "public_docs_notes",
        )
        if not all(
            isinstance(license_info.get(field), str) and license_info[field]
            for field in license_fields
        ):
            raise SeedAssetError(f"{asset_id}.license fields must be documented")
        if license_info["public_docs_status"] not in PUBLIC_DOCS_STATUSES:
            raise SeedAssetError(f"{asset_id}.license.public_docs_status is invalid")
        if not license_info["notice_url"].startswith("https://"):
            raise SeedAssetError(f"{asset_id}.license.notice_url must be an HTTPS URL")

        size_bytes = _positive_int(
            row.get("size_bytes"), field=f"{asset_id}.size_bytes"
        )
        max_bytes = _positive_int(row.get("max_bytes"), field=f"{asset_id}.max_bytes")
        max_unpacked = _positive_int(
            row.get("max_unpacked_bytes"), field=f"{asset_id}.max_unpacked_bytes"
        )
        if size_bytes > max_bytes:
            raise SeedAssetError(f"{asset_id}.size_bytes exceeds max_bytes")

        assets.append(
            SeedAsset(
                id=asset_id,
                profiles=tuple(profiles),
                urls=tuple(urls),
                sha256=digest.lower(),
                size_bytes=size_bytes,
                max_bytes=max_bytes,
                max_unpacked_bytes=max_unpacked,
                archive=archive,
                root=_relative_path(row.get("root"), field=f"{asset_id}.root"),
                required_files=tuple(
                    _relative_path(value, field=f"{asset_id}.required_files")
                    for value in required_files
                ),
                source=source,
                artifact_spdx=license_info["artifact_spdx"],
                media_status=license_info["media_status"],
                notice_url=license_info["notice_url"],
                attribution=license_info["attribution"],
                public_docs_status=license_info["public_docs_status"],
                public_docs_notes=license_info["public_docs_notes"],
                filename=filename,
            )
        )
    return tuple(assets)


def select_profile(
    profile: str,
    assets: Iterable[SeedAsset] | None = None,
    *,
    required_ids: Iterable[str] = (),
) -> tuple[SeedAsset, ...]:
    available_assets = assets if assets is not None else load_manifest()
    selected = tuple(asset for asset in available_assets if profile in asset.profiles)
    if not selected:
        raise SeedAssetError(f"seed asset profile has no assets: {profile}")
    missing = sorted(set(required_ids) - {asset.id for asset in selected})
    if missing:
        raise SeedAssetError(
            f"{profile} profile is missing required assets: " + ", ".join(missing)
        )
    return selected


def _validate_root(asset: SeedAsset, content_dir: Path) -> Path:
    root = content_dir.joinpath(*asset.root.parts)
    if not root.is_dir():
        raise SeedAssetError(f"{asset.id}: extracted root is missing: {asset.root}")
    resolved_content = content_dir.resolve()
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(resolved_content):
        raise SeedAssetError(f"{asset.id}: asset root escapes the content directory")
    for required in asset.required_files:
        candidate = root.joinpath(*required.parts)
        if not candidate.is_file() or not candidate.resolve().is_relative_to(
            resolved_root
        ):
            raise SeedAssetError(f"{asset.id}: required file is missing: {required}")
    return root


def _validate_cached(asset: SeedAsset, destination: Path) -> Path:
    archive = destination / "source.bin"
    if not archive.is_file() or archive.stat().st_size != asset.size_bytes:
        raise SeedAssetError(f"{asset.id}: cached source size is invalid")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != asset.sha256:
        raise SeedAssetError(f"{asset.id}: cached source SHA-256 is invalid")
    return _validate_root(asset, destination / "content")


def _member_path(name: str, *, asset_id: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise SeedAssetError(f"{asset_id}: unsafe archive path: {name}")
    return path


def _extract_zip(asset: SeedAsset, archive_path: Path, content_dir: Path) -> None:
    unpacked = 0
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = _member_path(member.filename, asset_id=asset.id)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SeedAssetError(
                    f"{asset.id}: archive symlink is not allowed: {path}"
                )
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise SeedAssetError(
                    f"{asset.id}: archive special file is not allowed: {path}"
                )
            unpacked += member.file_size
            if unpacked > asset.max_unpacked_bytes:
                raise SeedAssetError(f"{asset.id}: unpacked content exceeds size limit")
            target = content_dir.joinpath(*path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def _extract_tar(asset: SeedAsset, archive_path: Path, content_dir: Path) -> None:
    unpacked = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            path = _member_path(member.name, asset_id=asset.id)
            if member.issym() or member.islnk() or member.isdev():
                raise SeedAssetError(
                    f"{asset.id}: archive link/device is not allowed: {path}"
                )
            if not member.isdir() and not member.isfile():
                raise SeedAssetError(f"{asset.id}: unsupported archive member: {path}")
            unpacked += member.size
            if unpacked > asset.max_unpacked_bytes:
                raise SeedAssetError(f"{asset.id}: unpacked content exceeds size limit")
            target = content_dir.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise SeedAssetError(f"{asset.id}: cannot read archive member: {path}")
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def _prepare_content(asset: SeedAsset, download: Path, content_dir: Path) -> None:
    content_dir.mkdir(parents=True)
    if asset.archive == "none":
        assert asset.filename is not None
        if download.stat().st_size > asset.max_unpacked_bytes:
            raise SeedAssetError(f"{asset.id}: raw content exceeds unpacked size limit")
        target = content_dir / asset.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(download, target)
    elif asset.archive == "zip":
        _extract_zip(asset, download, content_dir)
    else:
        _extract_tar(asset, download, content_dir)


def _download(asset: SeedAsset, client: httpx.Client, target: Path) -> str:
    failures: list[str] = []
    for url in asset.urls:
        digest = hashlib.sha256()
        received = 0
        try:
            with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                if response.url.scheme != "https":
                    raise SeedAssetError("redirected download URL is not HTTPS")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > asset.max_bytes:
                    raise SeedAssetError(f"response exceeds {asset.max_bytes} bytes")
                with target.open("wb") as output:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > asset.max_bytes:
                            raise SeedAssetError(
                                f"download exceeds {asset.max_bytes} bytes"
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if received != asset.size_bytes:
                raise SeedAssetError(
                    f"size mismatch: expected {asset.size_bytes}, received {received}"
                )
            if digest.hexdigest() != asset.sha256:
                raise SeedAssetError("SHA-256 mismatch")
            return url
        except (httpx.HTTPError, SeedAssetError, ValueError) as exc:
            target.unlink(missing_ok=True)
            failures.append(f"{url}: {exc}")
    raise SeedAssetError(f"{asset.id}: all download URLs failed: {'; '.join(failures)}")


def ensure_asset(
    asset: SeedAsset,
    *,
    cache_dir: Path | None = None,
    asset_dir: Path | None = None,
    offline: bool = False,
    client: httpx.Client | None = None,
) -> ResolvedSeedAsset:
    if asset_dir is not None:
        local_content = asset_dir / asset.id
        root = _validate_root(asset, local_content)
        return ResolvedSeedAsset(asset=asset, root=root, source="local-override")

    cache = cache_dir or default_cache_dir()
    destination = cache / asset.id / asset.sha256
    marker = destination / ".complete.json"
    if marker.is_file():
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if metadata.get("sha256") == asset.sha256:
                root = _validate_cached(asset, destination)
                return ResolvedSeedAsset(asset=asset, root=root, source="cache")
        except (OSError, json.JSONDecodeError, SeedAssetError):
            pass
    if offline:
        raise SeedAssetError(
            f"{asset.id}: verified cache entry is unavailable in offline mode"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{asset.sha256[:12]}-", dir=destination.parent)
    )
    owned_client = client is None
    active_client = client or httpx.Client(
        headers={"User-Agent": DOWNLOAD_USER_AGENT},
        timeout=httpx.Timeout(60.0, connect=15.0),
    )
    try:
        download = staging / "download.part"
        source_url = _download(asset, active_client, download)
        staging_content = staging / "content"
        _prepare_content(asset, download, staging_content)
        root = _validate_root(asset, staging_content)
        download.replace(staging / "source.bin")
        (staging / ".complete.json").write_text(
            json.dumps(
                {
                    "asset_id": asset.id,
                    "sha256": asset.sha256,
                    "source_url": source_url,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        return ResolvedSeedAsset(
            asset=asset,
            root=destination / root.relative_to(staging),
            source=source_url,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if owned_client:
            active_client.close()


def ensure_profile(
    profile: str,
    *,
    cache_dir: Path | None = None,
    asset_dir: Path | None = None,
    offline: bool = False,
    assets: Iterable[SeedAsset] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, ResolvedSeedAsset]:
    return {
        asset.id: ensure_asset(
            asset,
            cache_dir=cache_dir,
            asset_dir=asset_dir,
            offline=offline,
            client=client,
        )
        for asset in select_profile(profile, assets)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("demo", "online-training", "legacy"), default="demo"
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--asset-dir", type=Path)
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    resolved = ensure_profile(
        args.profile,
        cache_dir=args.cache_dir,
        asset_dir=args.asset_dir,
        offline=args.offline,
    )
    for asset_id, item in resolved.items():
        print(f"{asset_id}\t{item.root}\t{item.source}")


if __name__ == "__main__":
    main()
