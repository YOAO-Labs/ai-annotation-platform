"""Explicit adoption of retained preview, evaluation and selection outputs.

Independent database receipts explain every accepted object. Verification never
reads a source, repairs an output, or proves that an old remote process stopped.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_storage_adoption import (
    CAPACITY,
    MAX_TRANSFER_BYTES,
    TIMEOUT_SECONDS,
    Reader,
    _observation,
    reject,
)

TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_ATTEMPTS = 10_000
PURPOSES = {"training-preview", "sample-evaluation", "active-learning"}


def _record(facts):
    values = deepcopy(facts)
    for key, value in values.items():
        if value is not None and (key == "id" or key.endswith("_id")):
            try:
                values[key] = uuid.UUID(value)
            except (ValueError, TypeError):
                pass
    return SimpleNamespace(**values)


def _stopped(state):
    record = state["record"]
    purpose = state["purpose"]
    allowed = TERMINAL | ({"expired"} if purpose == "training-preview" else set())
    if record["status"] not in allowed:
        reject("legacy_storage_version_active")
    if purpose == "training-preview":
        deadline = record["put_expires_at"]
        if deadline is not None and datetime.fromisoformat(deadline) > datetime.now(
            timezone.utc
        ):
            reject("legacy_storage_version_active")
        return
    attempts = state["attempts"]
    if len(attempts) > MAX_ATTEMPTS:
        reject("legacy_storage_inventory_limit")
    identifiers = set()
    current = record["current_attempt_id"]
    current_facts = None
    for attempt in attempts:
        identifier = str(uuid.UUID(attempt["id"]))
        if identifier != attempt["id"] or identifier in identifiers:
            reject()
        identifiers.add(identifier)
        if (
            type(attempt["generation"]) is not int
            or not 0 < attempt["generation"] <= record["generation"]
        ):
            reject()
        if identifier == current:
            current_facts = attempt
        if attempt["status"] in TERMINAL:
            continue
        if not (
            attempt["status"] == "lost"
            and identifier != current
            and type(attempt["generation"]) is int
            and attempt["generation"] < record["generation"]
            and attempt["claim_id"] is None
            and attempt["completed_at"] is not None
        ):
            reject("legacy_storage_version_active")
    if current is not None and current not in identifiers:
        reject()
    if current_facts is not None and (
        current_facts["generation"] != record["generation"]
        or current_facts["status"] != record["status"]
    ):
        reject("legacy_storage_version_active")
    if record["status"] == "succeeded" and current_facts is None:
        reject()


def _json(raw, *, nodes=200000, strings=64000):
    from app.services.training_artifacts import _bounded

    value = json.loads(raw)
    if not isinstance(value, dict):
        reject()
    _bounded(value, budget=[nodes], string_budget=strings)
    return value


def _png(raw, *, dimensions=None, maximum_dimension=2560):
    with Image.open(io.BytesIO(raw)) as image:
        if (
            image.format != "PNG"
            or min(image.size) < 1
            or max(image.size) > maximum_dimension
            or (dimensions is not None and image.size != dimensions)
        ):
            reject()
        image.verify()


def _receipt(value):
    if (
        not isinstance(value, dict)
        or type(value.get("size_bytes")) is not int
        or value["size_bytes"] <= 0
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in value["sha256"])
    ):
        reject()
    return value


def _bytes(reader, key, receipt, metadata, *, maximum):
    spec = _receipt(receipt)
    raw, evidence, head = reader.read(key, maximum=min(spec["size_bytes"], maximum))
    if (
        evidence["sha256"] != spec["sha256"]
        or evidence["size_bytes"] != spec["size_bytes"]
        or any(head.get("Metadata", {}).get(k) != v for k, v in metadata.items())
    ):
        reject()
    return raw


def _preview(reader, state, inventory):
    from app.schemas.training_preview import PreviewArtifact
    from app.services import (
        training_preview_storage as owners,
        training_previews as service,
    )
    from app.services.training_artifacts import MAX_REPORT_BYTES

    record = state["record"]
    expected, required = {}, set()
    uploads, published = record["upload_inventory"], record["artifacts"]
    count = record["spec"]["count"]
    if type(count) is not int or not 1 <= count <= service.LIMITS["outputs"]:
        reject()
    names = {"report.json"} | {f"preview-{i}.png" for i in range(count)}
    if (
        not isinstance(uploads, dict)
        or not isinstance(published, dict)
        or len(set(uploads) | set(published)) > 5
    ):
        reject()
    if not set(uploads) | set(published) <= names:
        reject("legacy_storage_unknown_object")
    for name in set(uploads) | set(published):
        key = owners.expect_artifact(reader.owner, name, reader.owner.key(name))
        receipt = uploads.get(name)
        if receipt is not None:
            receipt = PreviewArtifact.model_validate(
                {"name": name, **receipt}
            ).model_dump()
            if receipt["name"] != name:
                reject()
        if name in published:
            accepted = published[name]
            owners.expect_artifact(reader.owner, name, accepted["key"])
            accepted = PreviewArtifact.model_validate(
                {"name": name, **{k: v for k, v in accepted.items() if k != "key"}}
            ).model_dump()
            if accepted["name"] != name:
                reject()
            if receipt is not None and accepted != receipt:
                reject()
            receipt = accepted
            required.add(key)
        expected[key] = {**_receipt(receipt), "key": key}
    if record["status"] == "succeeded":
        required |= {reader.owner.key("report.json")} | {
            reader.owner.key(f"preview-{i}.png") for i in range(record["spec"]["count"])
        }
    _members(inventory, expected, required)
    content = {}
    for key in sorted(inventory):
        spec = expected[key]
        content[key] = _bytes(
            reader,
            key,
            spec,
            {
                "preview-id": record["id"],
                "input-digest": record["input_digest"],
                "sha256": spec["sha256"],
            },
            maximum=MAX_REPORT_BYTES
            if key.endswith("/report.json")
            else service.LIMITS["output_bytes"],
        )
    report_key = reader.owner.key("report.json")
    outputs = {}
    if report_key in content:
        artifacts = {
            key[len(reader.owner.prefix) :]: spec for key, spec in expected.items()
        }
        report = service.validate_report(
            _record(record), artifacts, _json(content[report_key])
        )
        outputs = {output.image: output for output in report.outputs}
    for key, raw in content.items():
        if key == report_key:
            continue
        output = outputs.get(key[len(reader.owner.prefix) :])
        _png(raw, dimensions=(output.width, output.height) if output else None)


def _members(inventory, expected, required):
    if not required <= set(inventory):
        reject("legacy_storage_required_copy_missing")
    if not set(inventory) <= set(expected):
        reject("legacy_storage_unknown_object")


def _evaluation(reader, state, inventory):
    from app.schemas.sample_evaluation import EvaluationReport
    from app.services import (
        sample_evaluation_storage as owners,
        sample_evaluations as service,
    )

    run = _record(state["record"])
    spec = service.validate_spec(run)
    expected = {}
    for facts in state["attempts"]:
        if facts["report"] is None:
            if facts["status"] == "succeeded":
                reject("legacy_storage_required_copy_missing")
            continue
        receipt = _receipt(facts["report"])
        key = owners.expect_report(
            reader.owner, uuid.UUID(facts["id"]), receipt["key"], receipt["sha256"]
        )
        expected[key] = (facts, receipt)
    _members(inventory, expected, set(expected))
    for key, (facts, receipt) in sorted(expected.items()):
        attempt = _record(facts)
        raw = _bytes(
            reader,
            key,
            receipt,
            service.report_metadata(run, attempt),
            maximum=service.MAX_REPORT_BYTES,
        )
        value = _json(raw, nodes=service.engine.REPORT_NODE_BUDGET, strings=8192)
        checked = EvaluationReport.model_validate(value).model_dump(
            mode="json", by_alias=True
        )
        identity = {
            "schema": spec["schema"],
            "evaluation_id": str(run.id),
            "attempt_id": facts["id"],
            "spec_digest": run.spec_digest,
            "benchmark_digest": spec["benchmark_digest"],
            "comparison_digest": spec["comparison_digest"],
            "model_ref": spec["model_ref"],
            "evaluator": spec["evaluator"],
            "total_samples": len(spec["samples"]),
            "status": facts["status"],
        }
        if any(checked[k] != v for k, v in identity.items()):
            reject()
        # Shared sample checkpoints may have changed during a later retry. Old
        # report identity remains independently explained by its publication
        # receipt; only the current attempt can reuse the current checkpoints.
        if facts["id"] == state["record"]["current_attempt_id"]:
            service.verify_report(
                checked, run, attempt, [_record(row) for row in state["members"]]
            )


def _learning(reader, state, inventory):
    from app.services import (
        active_learning_storage as owners,
        active_learning as service,
    )

    record = state["record"]
    spec = service.validate_spec(_record(record))
    samples = spec["samples"]
    if len(samples) > 32:
        reject("legacy_storage_inventory_limit")
    expected, required, seen = {}, set(), set()
    rows = state["members"]
    if len(rows) != len(samples):
        reject()
    for sample, row in zip(samples, rows, strict=True):
        ordinal = sample["ordinal"]
        if ordinal in seen or row["ordinal"] != ordinal:
            reject()
        seen.add(ordinal)
        key = owners.expect_media(
            reader.owner, ordinal, reader.owner.key(f"media/{ordinal}.png")
        )
        expected[key] = sample
        media = row["media"]
        if media is not None and media != {
            "key": key,
            "sha256": sample["derived_sha256"],
        }:
            reject()
        if (
            media is not None
            or row["request"] is not None
            or row["evidence"] is not None
            or row["status"] == "succeeded"
            or record["status"] == "succeeded"
        ):
            required.add(key)
    _members(inventory, expected, required)
    for key in sorted(inventory):
        sample = expected[key]
        raw, evidence, head = reader.read(key, maximum=service.detection.MAX_PNG_BYTES)
        if evidence["sha256"] != sample["derived_sha256"] or head.get(
            "Metadata", {}
        ) != {
            "round-id": record["id"],
            "ordinal": str(sample["ordinal"]),
            "source-sha256": sample["sha256"],
            "sha256": sample["derived_sha256"],
        }:
            reject()
        _png(
            raw,
            dimensions=(sample["width"], sample["height"]),
            maximum_dimension=service.detection.MAX_PIXELS,
        )


def verify(
    client,
    owner,
    state,
    *,
    max_bytes=MAX_TRANSFER_BYTES,
    timeout_seconds=TIMEOUT_SECONDS,
):
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or not 0 < timeout_seconds <= TIMEOUT_SECONDS
    ):
        reject("legacy_storage_invalid_budget")
    reader = Reader(client, owner, max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    try:
        if state["purpose"] not in PURPOSES:
            reject()
        _stopped(state)
        before = reader.inventory()
        {
            "training-preview": _preview,
            "sample-evaluation": _evaluation,
            "active-learning": _learning,
        }[state["purpose"]](reader, state, before)
        if reader.inventory() != before:
            reject("legacy_storage_changed")
        for key, observation in reader.heads.items():
            reader.check()
            if (
                _observation(client.head_object(Bucket=owner.bucket, Key=key))
                != observation
            ):
                reject("legacy_storage_changed")
        return {
            "schema": 1,
            "scope": "current-retained-copy",
            "purpose": state["purpose"],
            "context_digest": content_digest(state),
            "object_count": len(before),
            "read_bytes": max_bytes - reader.remaining,
            "inventory_digest": content_digest(
                sorted(reader.observed, key=lambda row: row["key"])
            ),
        }
    except TrainingDatasetError:
        raise
    except (BotoCoreError, ClientError):
        reject("legacy_storage_unavailable", 503)
    except (KeyError, ValueError, TypeError, OSError, AttributeError, OverflowError):
        reject()


def verify_operation(owner, state):
    from app.services.managed_original_creation import create_client

    client = create_client()
    try:
        return verify(client, owner, state)
    finally:
        try:
            client.close()
        except BaseException:
            reject("legacy_storage_close_unsettled", 503)


def _facts(row):
    return json.loads(
        json.dumps(
            {
                column.key: deepcopy(getattr(row, column.key))
                for column in row.__table__.columns
                if column.key != "storage_ref"
            },
            default=lambda value: (
                value.isoformat() if isinstance(value, datetime) else str(value)
            ),
        )
    )


def _kind(purpose):
    from app.db.models.training_preview import (
        TrainingPreview,
        TrainingPreviewResourceLease,
    )
    from app.db.models.sample_evaluation import (
        SampleEvaluationRun,
        SampleEvaluationAttempt,
        SampleEvaluationSample,
    )
    from app.db.models.active_learning import (
        ActiveLearningRound,
        ActiveLearningAttempt,
        ActiveLearningCandidate,
    )
    from app.services import (
        training_preview_storage,
        sample_evaluation_storage,
        active_learning_storage,
    )

    return {
        "training-preview": (
            TrainingPreview,
            None,
            TrainingPreviewResourceLease,
            None,
            training_preview_storage,
        ),
        "sample-evaluation": (
            SampleEvaluationRun,
            SampleEvaluationAttempt,
            SampleEvaluationSample,
            "run_id",
            sample_evaluation_storage,
        ),
        "active-learning": (
            ActiveLearningRound,
            ActiveLearningAttempt,
            ActiveLearningCandidate,
            "round_id",
            active_learning_storage,
        ),
    }[purpose]


async def pin(db, purpose, identifier, actor, project, key_id):
    """Fence one retained owner, then publish only after authority and I/O settle."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.db.models.api_key import ApiKey
    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.services import storage_access
    from app.services.content_snapshot import ContentError
    from app.services.dataset_content import key_authority
    from app.services.project_write_guard import lock_actor_project_authority
    from app.services.project_access import resolve_project_access_by_id

    model, attempt_model, member_model, relation, owners = _kind(purpose)
    scopes = ["annotations:read", "datasets:read", "datasets:write"]

    async def authority(*, lock_key):
        await lock_actor_project_authority(db, actor.id, project.id)
        _, access = await resolve_project_access_by_id(
            db, user=actor, project_id=project.id
        )
        if not access.is_manager:
            raise HTTPException(403, detail="项目管理权限已变化")
        if lock_key and key_id is not None:
            await db.execute(
                select(ApiKey.id).where(ApiKey.id == key_id).with_for_update(read=True)
            )
        return [await key_authority(db, actor, key_id, scope) for scope in scopes]

    async def context():
        row = await db.scalar(
            select(model)
            .where(model.id == identifier, model.project_id == project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, detail="训练输出记录不存在")
        if row.storage_ref is not None:
            return row, None
        attempts, members = [], []
        if attempt_model is None:
            if await db.scalar(
                select(member_model.resource_id)
                .where(member_model.preview_id == row.id)
                .limit(1)
            ):
                reject("legacy_storage_version_active")
        else:
            attempts = list(
                await db.scalars(
                    select(attempt_model)
                    .where(getattr(attempt_model, relation) == row.id)
                    .order_by(attempt_model.id)
                    .limit(MAX_ATTEMPTS + 1)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            if len(attempts) > MAX_ATTEMPTS:
                reject("legacy_storage_inventory_limit")
            members = list(
                await db.scalars(
                    select(member_model)
                    .where(getattr(member_model, relation) == row.id)
                    .order_by(member_model.ordinal)
                    .limit(33)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            )
            if len(members) > 32:
                reject("legacy_storage_inventory_limit")
        state = {
            "purpose": purpose,
            "record": _facts(row),
            "attempts": [_facts(item) for item in attempts],
            "members": [_facts(item) for item in members],
        }
        _stopped(state)
        return row, state

    try:
        initial_authority = await authority(lock_key=False)
        row, state = await context()
        if row.storage_ref is not None:
            owners.resolve(row)
            return row
        candidate = await owners.adoption_candidate(db, row)
        owner = owners.resolve_ref(
            candidate, expected_prefix=owners.prefix_for(project.id, row.id)
        )
        if not CAPACITY.acquire(blocking=False):
            reject("legacy_storage_verification_busy", 429)
        revision = None

        async def acquired(permit):
            nonlocal revision
            revision = permit.revision

        unsettled = False
        try:
            evidence = await storage_access.run(
                db,
                uuid.UUID(candidate["location_id"]),
                purpose.replace("-", "_") + "_storage_adoption",
                verify_operation,
                owner,
                state,
                on_acquired=acquired,
            )
        except BaseException as exc:
            unsettled = getattr(exc, "storage_operation_unsettled", False)
            raise
        finally:
            if not unsettled:
                CAPACITY.release()
        if await authority(lock_key=True) != initial_authority:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == uuid.UUID(candidate["location_id"]))
            .with_for_update(read=True)
        )
        await db.execute(
            select(StorageBinding.purpose)
            .where(StorageBinding.purpose == "training")
            .with_for_update(read=True)
        )
        current, current_state = await context()
        if (
            current.storage_ref is not None
            or content_digest(state) != content_digest(current_state)
            or candidate != await owners.adoption_candidate(db, current)
        ):
            reject("legacy_storage_context_changed")
        await storage_access.check(
            db,
            uuid.UUID(candidate["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        current.storage_ref = {
            **candidate,
            "evidence": {
                **evidence,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "actor_id": str(actor.id),
                "api_key_id": str(key_id) if key_id else None,
            },
        }
        await db.commit()
        return current
    except TrainingDatasetError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from None
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
