"""Read the configured platform S3 source into a private migration inventory.

This operator command never selects a target, copies objects or authorizes cutover.
Run with the intended API virtualenv and explicit deployment environment.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from storage_migration_inventory import (
    InventoryError,
    InventoryStopped,
    InventoryLimits,
    PURPOSES,
    verify_inventory,
    write_inventory,
)

from app.services.storage_migration_observations import (  # noqa: E402, F401
    binding_observations,
    collect_observations,
    endpoint_identity,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    command.add_argument("--handling", type=Path)
    command.add_argument("--label", default="Configured platform storage")
    for name in ("max-pages", "max-entries", "max-object-bytes", "max-total-bytes"):
        command.add_argument("--" + name, type=int)
    command.add_argument("--read-timeout-seconds", type=float, default=300)
    command.add_argument("--page-size", type=int, default=1000)
    return command


def handling_file(path):
    if path is None:
        return {}
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise InventoryError("invalid_handling")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InventoryError("invalid_handling")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise InventoryError("invalid_handling") from None
    if (
        not isinstance(value, dict)
        or not set(value) <= PURPOSES
        or any(item not in ("migrate", "rebuild", "retain") for item in value.values())
    ):
        raise InventoryError("invalid_handling")
    return value


async def configured_inventory(args, handling, limits):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    from botocore.config import Config
    from app.config import settings
    from app.db.base import engine
    from app.services.dataset_assets import (
        LEGACY_LOCATION_ID,
        PURPOSE_BUCKET_ATTRIBUTES,
    )
    from app.services.storage import StorageService

    storage = None
    try:
        # Constructing StorageService is read-free; never call ensure_all_buckets.
        storage = StorageService(
            client_config=Config(
                connect_timeout=min(10, limits.read_timeout_seconds),
                read_timeout=limits.read_timeout_seconds,
                retries={"total_max_attempts": 1},
            )
        )
        purposes = {
            purpose: getattr(storage, attribute)
            for purpose, attribute in PURPOSE_BUCKET_ATTRIBUTES.items()
        }
        endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        observations = await collect_observations(
            engine, purposes, endpoint, str(LEGACY_LOCATION_ID)
        )
        return write_inventory(
            args.output,
            storage.client,
            purposes,
            source={"label": args.label, "endpoint": endpoint},
            observations=observations,
            handling=handling,
            limits=limits,
        )
    finally:
        if storage is not None:
            storage.client.close()
        await engine.dispose()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        handling = handling_file(args.handling)
        values = {
            key: getattr(args, key)
            for key in (
                "max_pages",
                "max_entries",
                "max_object_bytes",
                "max_total_bytes",
            )
            if getattr(args, key) is not None
        }
        limits = InventoryLimits(
            **values,
            page_size=args.page_size,
            read_timeout_seconds=args.read_timeout_seconds,
        )
        report = (
            verify_inventory(args.verify)
            if args.verify
            else asyncio.run(configured_inventory(args, handling, limits))
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["complete"] else 1
    except (Exception, InventoryStopped) as exc:
        # Raw SDK, SQL and OS messages can contain object keys or credentials.
        code = (
            exc.code
            if isinstance(exc, (InventoryError, InventoryStopped))
            else "inventory_failed"
        )
        print(json.dumps({"complete": False, "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
