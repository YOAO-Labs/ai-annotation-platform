"""Plan, resume and verify an initial S3 copy; never switch application storage."""

import argparse
import json
from pathlib import Path
import signal
import sys

from storage_migration_copy import (
    Checkpoint,
    CopyError,
    Limits,
    error_code,
    execute,
    plan,
    private_json,
    target_config,
    validate_target,
)
from storage_migration_references import endpoint


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CopyError("invalid_arguments")


def parser():
    command = SafeParser(description=__doc__)
    modes = command.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "verify", "status"):
        mode = modes.add_parser(name)
        mode.add_argument("--checkpoint", type=Path, required=True)
        if name != "status":
            mode.add_argument("--target-config", type=Path, required=True)
        if name == "plan":
            mode.add_argument("--source-inventory", type=Path, required=True)
            mode.add_argument("--references", type=Path, required=True)
            mode.add_argument("--handling", type=Path)
        if name in {"plan", "run", "verify"}:
            for field in Limits.__dataclass_fields__:
                mode.add_argument(
                    "--" + field.replace("_", "-"),
                    type=float if field.endswith("seconds") else int,
                )
        if name == "run":
            mode.add_argument("--retry-failed", action="store_true")
    return command


def client(address, access, secret, limits):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=address,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(
            connect_timeout=limits.connect_timeout_seconds,
            read_timeout=limits.read_timeout_seconds,
            retries={"total_max_attempts": 1},
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path", "payload_signing_enabled": False},
        ),
    )


def configured_source(state, limits):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    from app.config import settings

    address = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    if endpoint(address) != endpoint(state.meta["source_endpoint"]):
        raise CopyError("source_identity_mismatch")
    return client(address, settings.minio_access_key, settings.minio_secret_key, limits)


def limits_from(args, baseline=None):
    values = baseline or {}
    return Limits(
        **(
            values
            | {
                k: getattr(args, k)
                for k in Limits.__dataclass_fields__
                if getattr(args, k, None) is not None
            }
        )
    )


def main(argv=None):
    source = target = None
    try:
        args = parser().parse_args(argv)
        if args.command == "status":
            with Checkpoint(args.checkpoint) as state:
                result = state.status()
        else:
            config = target_config(args.target_config)
            if args.command == "plan":
                result = plan(
                    args.checkpoint,
                    args.source_inventory,
                    args.references,
                    config,
                    handling=private_json(args.handling) if args.handling else None,
                    limits=limits_from(args),
                )
            else:
                with Checkpoint(args.checkpoint) as state:
                    validate_target(state, config)
                    limits = limits_from(args, state.meta["limits"])
                    if args.command == "run":
                        source = configured_source(state, limits)
                    target = client(
                        config["endpoint_url"],
                        config["access_key_id"],
                        config["secret_access_key"],
                        limits,
                    )
                    stopped = False

                    def pause(signum, frame):
                        nonlocal stopped
                        stopped = True

                    original = {
                        sig: signal.signal(sig, pause)
                        for sig in (signal.SIGINT, signal.SIGTERM)
                    }
                    try:
                        result = execute(
                            state,
                            source,
                            target,
                            limits=limits,
                            retry_failed=getattr(args, "retry_failed", False),
                            verify_only=args.command == "verify",
                            stopped=lambda: stopped,
                        )
                    finally:
                        for sig, handler in original.items():
                            signal.signal(sig, handler)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["copy_subset_complete"] and not result.get("paused") else 1
    except Exception as exc:
        print(
            json.dumps({"error": error_code(exc), "migration_ready": False}),
            file=sys.stderr,
        )
        return 2
    finally:
        close_failed = False
        for item in (source, target):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    close_failed = True
        if close_failed:
            print(
                json.dumps({"error": "client_close_failed", "migration_ready": False}),
                file=sys.stderr,
            )
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
