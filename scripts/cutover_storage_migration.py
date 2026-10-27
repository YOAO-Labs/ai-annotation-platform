"""Atomically switch nine purposes and resume a retained-object rollback.

Run with the trusted deployment operator's application environment. Native
Docker authority and the two independent fence credentials stay in this CLI.
The target remains physically read-only after rollback; saved B locators are
never rewritten to A or discarded by a database snapshot restore.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from finalize_storage_migration import (
    identifier,
    matching_claim,
    native_proof,
    positive,
    safe_error,
    stable_native,
    transaction,
)
from storage_source_fence import bound_state, descriptor
from storage_source_fence_docker import DockerAdapter
from storage_source_fence_state import Controller, FenceError, Journal

SCHEMA = "aap.storage-migration-cutover-cli/v1"
ROLLBACK_CLAIM_SCHEMA = "aap.storage-migration-rollback-fence-claim/v1"


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise FenceError("invalid_arguments")


def parser():
    command = SafeParser(description=__doc__)
    modes = command.add_subparsers(dest="command", required=True)
    for name in (
        "preview",
        "activate",
        "status",
        "begin-rollback",
        "bind-rollback",
        "restore-source",
    ):
        mode = modes.add_parser(name)
        mode.add_argument("--actor-id", type=identifier, required=True)
        mode.add_argument("--cutover-id", type=identifier, required=True)
        if name in {"preview", "activate", "restore-source"}:
            mode.add_argument("--config", type=Path, required=True)
            mode.add_argument("--state", type=Path, required=True)
        if name in {"bind-rollback", "restore-source"}:
            mode.add_argument("--target-config", type=Path, required=True)
            mode.add_argument("--target-state", type=Path, required=True)
        if name in {"preview", "activate"}:
            mode.add_argument("--review-file", type=Path, required=True)
        if name == "preview":
            mode.add_argument(
                "--expected-finalization-revision", type=positive, required=True
            )
            mode.add_argument(
                "--expected-publication-revision", type=positive, required=True
            )
        if name in {"begin-rollback", "bind-rollback", "restore-source"}:
            mode.add_argument("--expected-revision", type=positive, required=True)
    return command


def application():
    from app.services import (
        storage_migration_cutovers as service,
        storage_migration_finalizations as finalizations,
        storage_migration_read_publications as reads,
        storage_migration_windows as windows,
    )
    from app.services.prediction_inputs import settled_io
    from app.workers._db import task_session

    return service, finalizations, reads, windows, task_session, settled_io


def public_item(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: public_item(item)
            for key, item in value.items()
            if key != "impact_token"
        }
    if isinstance(value, (list, tuple)):
        return [public_item(item) for item in value]
    return value


def write_review(path, value):
    """Create once in a private directory; never overwrite an operator review."""
    encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode() + b"\n"
    if len(encoded) > 65536:
        raise FenceError("review_budget")
    path = Path(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise FenceError("review_directory_private")
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def target_native_proof(journal, adapter, config):
    before = adapter.application_endpoint()
    proof = native_proof(journal, adapter, config)
    if before != adapter.application_endpoint():
        raise FenceError("native_source_changed")
    proof["source"]["endpoint"] = before
    return proof


async def probe_targets(session, context):
    from app.db.models.dataset_asset import StorageLocation
    from app.services import managed_s3_target, storage_access
    from app.services import (
        annotations_storage_target,
        audit_archive_storage_target,
        avatar_storage_target,
        bug_attachment_storage_target,
        dataset_storage_target,
        export_storage_target,
        import_storage_target,
        media_cache_storage_target,
        training_storage_target,
    )

    probes = {
        "annotations": annotations_storage_target.probe_target,
        "audit-archive": audit_archive_storage_target.probe_target,
        "avatars": avatar_storage_target.probe_target,
        "bug-reports": bug_attachment_storage_target.probe_target,
        "datasets": dataset_storage_target.probe_target,
        "export": export_storage_target.probe_target,
        "import": import_storage_target.probe_target,
        "media-cache": media_cache_storage_target.probe_target,
        "training": training_storage_target.probe_target,
    }
    if set(context["targets"]) != set(probes):
        raise FenceError("cutover_targets_changed")
    capabilities = {}
    for purpose in sorted(probes):
        facts = context["targets"][purpose]
        async with session() as db:
            location = await db.get(StorageLocation, uuid.UUID(facts["location_id"]))
            target = await managed_s3_target.target_for(db, location, purpose=purpose)
            if target.facts != facts:
                raise FenceError("cutover_targets_changed")
            # No metadata transaction survives provider I/O. Admission is an
            # independent durable operation, and run drains real cancellation.
            await db.commit()
            capabilities[purpose] = await storage_access.run(
                db,
                target.location_id,
                "migration_cutover_probe",
                probes[purpose],
                target,
                expected_revision=context["target_gate_revision"],
            )
    return capabilities


def same_source(native, expected):
    from app.services.storage_migration_observations import endpoint_identity

    if (
        endpoint_identity(native["source"]["endpoint"])
        != endpoint_identity(expected["endpoint"])
        or native["source"]["purpose_buckets"] != expected["purpose_buckets"]
    ):
        raise FenceError("native_source_changed")


def rollback_claim(journal, cutover_id):
    claim = deepcopy(journal.data.get("rollback_claim"))
    if claim is None or claim["cutover_id"] != cutover_id:
        raise FenceError("rollback_claim_changed")
    return claim


async def close_source_window(session, windows, actor, window_id):
    from app.db.models.user import User
    from app.schemas.storage_migration_window import StorageMigrationWindowTransition

    async with session() as db:
        user = await db.get(User, actor, populate_existing=True)
        row = await windows.owner(db, uuid.UUID(window_id))
        result = await windows.transition(
            db,
            row.id,
            user,
            StorageMigrationWindowTransition(
                action="close", expected_revision=row.revision
            ),
        )
        await db.commit()
    if result.state != "closed":
        raise FenceError("migration_window_unsettled")


async def execute(args, source=None, target=None):
    service, finalizations, reads, windows, session, settled_io = application()
    actor, cutover_id = uuid.UUID(args.actor_id), uuid.UUID(args.cutover_id)

    async def call(method, *values):
        return await transaction(session, method, *values)

    if args.command == "status":
        return await call(service.status, actor, cutover_id)
    if args.command == "begin-rollback":
        return await call(
            service.begin_rollback, actor, cutover_id, args.expected_revision
        )

    if args.command in {"preview", "activate"}:
        journal, adapter, config = source
        claim = matching_claim(journal, args.cutover_id)
        before = await settled_io(native_proof, journal, adapter, config)
        if args.command == "activate":
            review, _ = descriptor(args.review_file)
            return await call(service.activate, actor, claim, review, before)
        values = (
            actor,
            claim,
            args.expected_finalization_revision,
            args.expected_publication_revision,
        )
        context = await call(service.prepare_activation, *values, before)
        capabilities = await probe_targets(session, context)
        after = await settled_io(native_proof, journal, adapter, config)
        stable_native(before, after)
        current = await call(service.prepare_activation, *values, after)
        review = service.issue_review(context, current, capabilities)
        write_review(args.review_file, review)
        return review

    journal_b, adapter_b, config_b = target
    native_b = await settled_io(target_native_proof, journal_b, adapter_b, config_b)
    if args.command == "bind-rollback":
        existing = journal_b.data.get("rollback_claim")
        if existing is None:
            # The physical observation is complete before a short DB lock. An
            # unknown commit retains this durable intent and never releases B.
            async with session() as db:
                context = await service.rollback_preflight(
                    db, actor, cutover_id, args.expected_revision
                )
                same_source(native_b, context["target_source"])
                claim_b = {
                    "schema": ROLLBACK_CLAIM_SCHEMA,
                    "cutover_id": args.cutover_id,
                    "expected_cutover_revision": context["revision"],
                    "database_identity": context["database_identity"],
                    "reservation_nonce": str(uuid.uuid4()),
                    "native_operation_id": native_b["operation_id"],
                    "config_digest": journal_b.config_digest,
                    "reserved": False,
                }
                journal_b.bind_rollback(claim_b)
                result = await service.bind_rollback(
                    db, actor, cutover_id, args.expected_revision, claim_b, native_b
                )
                await db.commit()
        else:
            claim_b = rollback_claim(journal_b, args.cutover_id)
            result = await call(
                service.bind_rollback,
                actor,
                cutover_id,
                args.expected_revision,
                claim_b,
                native_b,
            )
        journal_b.mark_rollback_reserved(
            claim_b["cutover_id"], claim_b["reservation_nonce"]
        )
        return result

    claim_b = rollback_claim(journal_b, args.cutover_id)
    current = await call(service.status, actor, cutover_id)
    if current["revision"] != args.expected_revision:
        raise FenceError("cutover_changed")
    saved_b = current["rollback_claim"]
    if saved_b is None or {
        key: value for key, value in claim_b.items() if key != "reserved"
    } != {key: value for key, value in saved_b.items() if key != "reserved"}:
        raise FenceError("rollback_claim_changed")
    # Compare this fresh B proof with its durable reservation before any A
    # action, so a changed gateway address cannot be discovered after release.
    current = await call(
        service.bind_rollback,
        actor,
        cutover_id,
        current["revision"],
        claim_b,
        native_b,
    )
    release_id = current["source_release_operation_id"] or str(uuid.uuid4())
    current = await call(
        service.begin_restoration, actor, cutover_id, current["revision"], release_id
    )
    journal_a, adapter_a, config_a = source
    claim_a = deepcopy(current["source_claim"])
    if journal_a.data.get("application_claim") is not None:
        held = matching_claim(journal_a, args.cutover_id)
        if {key: value for key, value in held.items() if key != "reserved"} != {
            key: value for key, value in claim_a.items() if key != "reserved"
        }:
            raise FenceError("application_claim_changed")
    if (
        journal_a.config_digest != claim_a["config_digest"]
        or journal_a.data["operation_id"] != claim_a["native_operation_id"]
    ):
        raise FenceError("native_source_changed")
    publication = await call(reads.status, actor, claim_a)
    if publication.state != "withdrawn":
        publication = await call(
            reads.begin_withdrawal, actor, claim_a, publication.revision
        )
        if publication.state != "withdrawn":
            publication = await call(
                reads.finish_withdrawal, actor, claim_a, publication.revision
            )
        if publication.state != "withdrawn":
            raise FenceError("read_publication_unsettled")
    finalization = await call(finalizations.status, actor, claim_a)
    if finalization.state != "invalidated":
        finalization = await call(
            finalizations.begin_invalidation, actor, claim_a, finalization.revision
        )
        if finalization.state != "invalidated":
            finalization = await call(
                finalizations.finish_invalidation, actor, claim_a, finalization.revision
            )
        if finalization.state != "invalidated":
            raise FenceError("finalization_unsettled")
    if journal_a.data.get("application_claim") is not None:
        journal_a.unbind_application(args.cutover_id, claim_a["reservation_nonce"])
    # The saved UUID precedes all external source mutation. Even a released
    # Journal replays verify_open before application writes may resume.
    await settled_io(
        Controller(journal_a, adapter_a).release, journal_a.data["revision"], release_id
    )
    await close_source_window(session, windows, actor, current["window_id"])
    after_b = await settled_io(target_native_proof, journal_b, adapter_b, config_b)
    stable_native(native_b, after_b)
    journal_b.mark_rollback_reserved(
        claim_b["cutover_id"], claim_b["reservation_nonce"]
    )
    return await call(
        service.finish_rollback, actor, cutover_id, current["revision"], after_b
    )


def open_fence(stack, config_path, state_path, *, target=False):
    config, digest = descriptor(config_path)
    adapter = (
        DockerAdapter(config, credential_prefix="AAP_TARGET_FENCE")
        if target
        else DockerAdapter(config)
    )
    bound_state(adapter.directory, state_path)
    journal = stack.enter_context(Journal(state_path, digest))
    return journal, adapter, config


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        with ExitStack() as stack:
            source = (
                open_fence(stack, args.config, args.state)
                if hasattr(args, "config")
                else None
            )
            target = (
                open_fence(stack, args.target_config, args.target_state, target=True)
                if hasattr(args, "target_config")
                else None
            )
            result = asyncio.run(execute(args, source, target))
            output = {"schema": SCHEMA, "item": public_item(result)}
        print(json.dumps(output, sort_keys=True, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "interrupted"}), file=sys.stderr)
        return 130
    except Exception as exc:
        code = safe_error(exc)
        if code == "finalization_failed":
            code = "cutover_failed"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
