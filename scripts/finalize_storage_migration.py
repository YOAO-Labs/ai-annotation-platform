"""Bind a real native source fence to an application's final migration evidence.

Run as a trusted deployment operator with the existing application database
environment. API and Worker processes never receive Docker authority or this
command's native credentials. No command changes business object routing.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from storage_source_fence import bound_state, descriptor
from storage_source_fence_docker import DockerAdapter
from storage_source_fence_state import Controller, FenceError, Journal

SCHEMA = "aap.storage-migration-finalization-cli/v1"
CLAIM_SCHEMA = "aap.storage-migration-fence-claim/v1"
SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise FenceError("invalid_arguments")


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise FenceError("invalid_identifier") from None
    return value


def revision(value, *, minimum):
    try:
        number = int(value)
        if number < minimum:
            raise ValueError
    except (ValueError, TypeError):
        raise FenceError("invalid_revision") from None
    return number


def positive(value):
    return revision(value, minimum=1)


def nonnegative(value):
    return revision(value, minimum=0)


def parser():
    command = SafeParser(description=__doc__)
    modes = command.add_subparsers(dest="command", required=True)
    for name in ("bind", "seal", "invalidate", "status"):
        mode = modes.add_parser(name)
        mode.add_argument("--config", type=Path, required=True)
        mode.add_argument("--state", type=Path, required=True)
        mode.add_argument("--actor-id", type=identifier, required=True)
        mode.add_argument("--finalization-id", type=identifier, required=True)
        if name == "bind":
            mode.add_argument("--window-id", type=identifier, required=True)
            mode.add_argument(
                "--expected-window-revision", type=positive, required=True
            )
            mode.add_argument(
                "--expected-native-revision", type=positive, required=True
            )
        elif name in {"seal", "invalidate"}:
            mode.add_argument(
                "--expected-revision",
                type=nonnegative if name == "invalidate" else positive,
                required=True,
            )
        if name == "seal":
            mode.add_argument("--copy-id", type=identifier, required=True)
    return command


def application():
    # Keep application settings and the database entirely out of native-only CLI
    # startup. The existing runtime launcher supplies the intended database.
    from app.services import storage_migration_finalizations as service
    from app.services.prediction_inputs import settled_io
    from app.services.storage_migration_finalization_evidence import (
        build_final_evidence,
    )
    from app.workers._db import task_session

    return service, task_session, settled_io, build_final_evidence


async def transaction(session, call, *args):
    async with session() as db:
        result = await call(db, *args)
        await db.commit()
        return result


def native_proof(journal, adapter, config):
    observed = Controller(journal, adapter).status()
    if not observed["held"]:
        raise FenceError("native_fence_not_held")
    barriers = json.dumps(
        journal.data["barriers"], sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "config_digest": journal.config_digest,
        "operation_id": journal.data["operation_id"],
        "journal_revision": observed["revision"],
        "barriers_sha256": hashlib.sha256(barriers).hexdigest(),
        "observed_at": observed["observed_at"],
        "counts": dict(observed["counts"]),
        "source": {
            "endpoint": config["public_endpoint"],
            "purpose_buckets": dict(config["purpose_buckets"]),
        },
    }


def stable_native(before, after):
    fields = ("config_digest", "operation_id", "barriers_sha256", "source", "counts")
    if any(before[key] != after[key] for key in fields):
        raise FenceError("native_fence_changed")


def matching_claim(journal, finalization_id):
    claim = deepcopy(journal.data.get("application_claim"))
    if claim is None or claim["finalization_id"] != finalization_id:
        raise FenceError("application_claim_changed")
    return claim


async def execute(args, journal, adapter, config):
    service, session, settled_io, evidence = application()

    async def call(method, *values):
        return await transaction(session, method, *values)

    actor = uuid.UUID(args.actor_id)
    if args.command == "bind":
        if journal.data["revision"] != args.expected_native_revision:
            raise FenceError("revision_changed")
        existing = journal.data.get("application_claim")
        native = native_proof(journal, adapter, config)
        if existing is None:
            from app.services.storage_migration_observations import endpoint_identity

            # Retain the short application locks through one private fsync and
            # the reservation commit, so close cannot overtake a new intent.
            # All native observations finish before opening this transaction.
            async with session() as db:
                context = await service.preflight(
                    db, actor, uuid.UUID(args.window_id), args.expected_window_revision
                )
                if (
                    endpoint_identity(native["source"]["endpoint"])
                    != endpoint_identity(context["source"]["endpoint"])
                    or native["source"]["purpose_buckets"]
                    != context["source"]["purpose_buckets"]
                ):
                    raise FenceError("native_source_changed")
                claim = {
                    "schema": CLAIM_SCHEMA,
                    "finalization_id": args.finalization_id,
                    "window_id": args.window_id,
                    "expected_window_revision": args.expected_window_revision,
                    "database_identity": context["database_identity"],
                    "reservation_nonce": str(uuid.uuid4()),
                    "native_operation_id": native["operation_id"],
                    "config_digest": journal.config_digest,
                    "reserved": False,
                }
                journal.bind_application(claim)
                result = await service.bind(db, actor, claim, native)
                await db.commit()
        else:
            claim = matching_claim(journal, args.finalization_id)
            if (
                claim["window_id"] != args.window_id
                or claim["expected_window_revision"] != args.expected_window_revision
            ):
                raise FenceError("application_claim_changed")
            result = await call(service.bind, actor, claim, native)
        # A failed/unknown database commit never removes the durable intent.
        journal.mark_application_reserved(
            claim["finalization_id"], claim["reservation_nonce"]
        )
        return result

    claim = matching_claim(journal, args.finalization_id)
    if args.command == "invalidate":
        result = await call(
            service.begin_invalidation, actor, claim, args.expected_revision
        )
        if result.state != "invalidated":
            result = await call(
                service.finish_invalidation, actor, claim, result.revision
            )
        if result.state != "invalidated":
            raise FenceError("finalization_unsettled")
        # Commit invalidation before removing the native release interlock.
        journal.unbind_application(claim["finalization_id"], claim["reservation_nonce"])
        return result

    before = native_proof(journal, adapter, config)
    if args.command == "status":
        return await call(service.status, actor, claim)

    prepared = await call(
        service.begin_seal,
        actor,
        claim,
        uuid.UUID(args.copy_id),
        args.expected_revision,
        before,
    )
    # Native/file work runs without a database transaction. Drain the real
    # thread through cancellation before releasing the Journal's physical lock.
    proof = await settled_io(
        evidence,
        prepared["values"],
        window_id=claim["window_id"],
        window_revision=prepared["item"].window_revision,
    )
    after = native_proof(journal, adapter, config)
    stable_native(before, after)
    return await call(
        service.finish_seal,
        actor,
        claim,
        prepared["item"].revision,
        prepared["token"],
        proof,
        after,
    )


def safe_error(exc):
    value = getattr(exc, "code", None)
    if value is None and isinstance(getattr(exc, "detail", None), dict):
        value = exc.detail.get("code")
    return (
        value
        if isinstance(value, str) and SAFE_CODE.fullmatch(value)
        else "finalization_failed"
    )


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        config, digest = descriptor(args.config)
        adapter = DockerAdapter(config)
        bound_state(adapter.directory, args.state)
        with Journal(args.state, digest) as journal:
            result = asyncio.run(execute(args, journal, adapter, config))
            output = {
                "schema": SCHEMA,
                "item": result.model_dump(mode="json"),
                "native_revision": journal.data["revision"],
            }
        print(json.dumps(output, sort_keys=True, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "interrupted"}), file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({"ok": False, "code": safe_error(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
