#!/usr/bin/env python
"""Administrative preparation for public YOLO weights.

Reviewed --asset-manifest mode imports verified local bytes or reads the
environment's authoritative bucket and atomically materializes a node cache.
Optional --publish conditionally creates immutable objects and verifies them.
This mode never imports a model framework or acquires a GPU lease.

Legacy variant selection (--task detect --series yolo11 --size s, or --all)
retains the existing upstream-download workflow for uncontrolled built-ins.
"""

from __future__ import annotations

import argparse
import hashlib
from functools import partial
import os
import sys
import time
from pathlib import Path

# 让此脚本独立可跑 (不依赖 backend 进程已起).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model_registry import (  # noqa: E402
    MODEL_MATRIX,
    UnsupportedVariantError,
    iter_supported_combinations,
    resolve_weight_filename,
)
from training_contract.assets import (  # noqa: E402
    asset_object_key,
    prepare_asset,
    read_manifest,
)


def _storage_client(args):
    # This is an administrator-only dependency already supplied by the API
    # environment. Runtime consumers never receive object-store credentials.
    import boto3  # noqa: PLC0415

    endpoint = args.endpoint or os.environ.get("MINIO_ENDPOINT")
    if not endpoint:
        raise ValueError("A reachable object-storage endpoint is required")
    if not endpoint.startswith(("http://", "https://")):
        secure = os.environ.get("MINIO_USE_SSL", "false").lower() == "true"
        endpoint = ("https://" if secure else "http://") + endpoint
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY"),
        aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=15,
            read_timeout=120,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _object_chunks(client, bucket, reference):
    response = client.get_object(Bucket=bucket, Key=asset_object_key(reference.sha256))
    body = response["Body"]
    try:
        yield from iter(lambda: body.read(1024 * 1024), b"")
    finally:
        body.close()


def _file_chunks(path):
    with path.open("rb") as source:
        yield from iter(lambda: source.read(1024 * 1024), b"")


def _verify_object(client, bucket, reference):
    checksum = hashlib.sha256()
    size = 0
    for chunk in _object_chunks(client, bucket, reference):
        size += len(chunk)
        if size > reference.size_bytes:
            raise ValueError("Authoritative object byte count differs")
        checksum.update(chunk)
    if size != reference.size_bytes or checksum.hexdigest() != reference.sha256:
        raise ValueError("Authoritative object differs from its immutable identity")


def _publish_object(client, bucket, reference, path):
    from botocore.exceptions import ClientError  # noqa: PLC0415

    key = asset_object_key(reference.sha256)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            raise
        try:
            with path.open("rb") as source:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=source,
                    ContentLength=reference.size_bytes,
                    ContentType="application/octet-stream",
                    Metadata={"sha256": reference.sha256},
                    IfNoneMatch="*",
                )
        except ClientError as conflict:
            if conflict.response.get("Error", {}).get("Code") not in {
                "412",
                "PreconditionFailed",
            }:
                raise
    # Never trust a matching name, size, ETag or custom metadata alone.
    _verify_object(client, bucket, reference)


def _prepare_manifest(args, target_dir):
    references = read_manifest(args.asset_manifest)
    if not references:
        raise ValueError("Asset manifest contains no assets")
    if args.from_bucket == bool(args.source_dir):
        raise ValueError("Select exactly one of --source-dir or --from-bucket")
    bucket = args.bucket or os.environ.get("MINIO_TRAINING_BUCKET", "training")
    client = _storage_client(args) if args.from_bucket or args.publish else None
    for reference in references:
        if reference.size_bytes is None:
            raise ValueError("Controlled asset preparation requires size_bytes")
        if args.from_bucket:
            source = partial(_object_chunks, client, bucket, reference)
        else:
            directory = Path(args.source_dir)
            candidate = directory / f"{reference.sha256}.pt"
            if not candidate.exists():
                candidate = directory / reference.filename
            source = partial(_file_chunks, candidate)
        for attempt in range(3):
            try:
                path = prepare_asset(target_dir, reference, source)
                if args.publish:
                    _publish_object(client, bucket, reference, path)
                break
            except Exception:
                if attempt == 2:
                    # HTTP/storage exceptions can contain signed credentials.
                    raise RuntimeError(
                        f"Could not prepare immutable asset {reference.filename}"
                    ) from None
                time.sleep(0.2 * (attempt + 1))
        print(f"ready {reference.filename} sha256={reference.sha256}")
    print(f"done: {len(references)} controlled assets ready in {target_dir}")
    return 0


def _download_one(task: str, series: str, size: str, target_dir: Path) -> bool:
    try:
        filename = resolve_weight_filename(task, series, size)
    except UnsupportedVariantError as exc:
        print(f"skip {task}/{series}/{size}: {exc}", file=sys.stderr)
        return False
    weight_path = target_dir / filename
    if weight_path.exists():
        print(f"hit  {filename}")
        return True

    from ultralytics import YOLO  # noqa: PLC0415

    cwd = os.getcwd()
    try:
        os.chdir(target_dir)
        print(f"pull {filename} ...")
        YOLO(filename)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"fail {filename}: {exc}", file=sys.stderr)
        return False
    finally:
        os.chdir(cwd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=list(MODEL_MATRIX.keys()), default=None)
    parser.add_argument("--series", default=None)
    parser.add_argument("--size", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--target-dir",
        default=os.environ.get("YOLO_CHECKPOINTS_DIR", "/app/checkpoints"),
    )
    parser.add_argument(
        "--asset-manifest",
        type=Path,
        help="Reviewed JSON asset manifest; controlled mode does not import a native loader",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Import verified local weights into the digest cache",
    )
    parser.add_argument(
        "--from-bucket",
        action="store_true",
        help="Prepare this node from the authoritative training bucket",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Verify or conditionally create immutable authoritative objects",
    )
    parser.add_argument(
        "--bucket", help="Existing MINIO_TRAINING_BUCKET (default: training)"
    )
    parser.add_argument("--endpoint", help="Reachable existing object-storage endpoint")
    args = parser.parse_args()

    target_dir = Path(args.target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if args.asset_manifest:
        if args.all or args.task or args.series or args.size:
            parser.error(
                "Controlled manifests cannot be combined with legacy variant selection"
            )
        try:
            return _prepare_manifest(args, target_dir)
        except Exception as exc:
            # Keep administrator errors useful without exposing storage URLs.
            print(
                f"Controlled asset preparation failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
    if (
        args.source_dir
        or args.from_bucket
        or args.publish
        or args.bucket
        or args.endpoint
    ):
        parser.error("Controlled preparation options require --asset-manifest")

    combos = iter_supported_combinations()
    if not args.all:
        if args.task:
            combos = [c for c in combos if c[0] == args.task]
        if args.series:
            combos = [c for c in combos if c[1] == args.series]
        if args.size:
            combos = [c for c in combos if c[2] == args.size]

    ok = 0
    for task, series, size in combos:
        if _download_one(task, series, size, target_dir):
            ok += 1
    print(f"done: {ok}/{len(combos)} weights ready in {target_dir}")
    return 0 if ok == len(combos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
