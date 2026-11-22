#!/usr/bin/env python3
"""Prepare reviewed controlled model assets without importing model frameworks.

Use --source-dir to import local bytes, or --from-bucket to populate a node
cache. --publish conditionally creates and verifies authoritative S3 objects.
"""

from __future__ import annotations

import argparse
import hashlib
from functools import partial
import os
import sys
import time
from pathlib import Path

from training_contract.assets import asset_object_key, prepare_asset, read_manifest


def _storage_client(args):
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
                    raise RuntimeError(
                        f"Could not prepare immutable asset {reference.filename}"
                    ) from None
                time.sleep(0.2 * (attempt + 1))
        print(f"ready {reference.filename} sha256={reference.sha256}")
    print(f"done: {len(references)} controlled assets ready in {target_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-manifest", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-dir", type=Path, help="Import verified local weights")
    source.add_argument(
        "--from-bucket",
        action="store_true",
        help="Read from the authoritative training bucket",
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
    parser.add_argument(
        "--target-dir",
        default=os.environ.get("YOLO_CHECKPOINTS_DIR", "/app/checkpoints"),
    )
    args = parser.parse_args()
    target_dir = Path(args.target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _prepare_manifest(args, target_dir)
    except Exception as exc:
        print(
            f"Controlled asset preparation failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
