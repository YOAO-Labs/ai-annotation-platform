"""Fenced bounded observations without execution-state or lease mutations."""

from __future__ import annotations

from app.services import training_storage_io as storage_io
from app.services import training_write_accounting as write_accounting
from contextlib import nullcontext
from app.services import storage_access

import hashlib
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select, tuple_
from training_contract.observability import (
    CHUNK_BYTES,
    LOG_BYTES,
    LOG_LIMIT,
    METRICS,
    REDACTION_REVISION,
    SAMPLE_LIMIT,
    STATE_BYTES,
    SUMMARY_BYTES,
    RedactedLines,
    encoded,
    redact_text,
)

from app.config import settings
from app.db.models.training_observability import (
    TrainingLogChunk,
    TrainingTelemetrySample,
)
from app.db.models.training_run import TrainingAttempt, TrainingResourceLease
from app.services import training_runs as training
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest

STREAMS = {
    "telemetry": (TrainingTelemetrySample, SAMPLE_LIMIT),
    "logs": (TrainingLogChunk, LOG_LIMIT),
}


def conflict(code, message):
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


async def ingest(db, attempt_id, body):
    lease, run, attempt = await training.fenced(
        db, attempt_id, body, allow_terminal=True
    )
    if attempt.status not in training.TERMINAL and lease.attempt_id != attempt.id:
        conflict("stale_attempt", "GPU 租约已变化，观测不能恢复执行所有权")
    await training.authority(db, run)
    state = dict(attempt.observation_state or {})
    stream_id = str(body.stream_id)
    if state and state["stream_id"] != stream_id:
        conflict(
            "observation_stream_conflict", "执行尝试已有持久观测流，不能从零重编号"
        )
    request_digest = content_digest(body.model_dump(mode="json"))
    receipt = state.get("receipt")
    if receipt and receipt["request_id"] == str(body.request_id):
        if receipt["digest"] != request_digest:
            conflict("observation_request_conflict", "同一观测请求不能改写内容")
        return {
            "stream_id": stream_id,
            "request_id": str(body.request_id),
            **receipt["cursors"],
        }
    state.setdefault("stream_id", stream_id)
    secrets = (settings.training_executor_token, body.lease_token)
    for name, (model, keep) in STREAMS.items():
        cursor = state.get(name + "_cursor", 0)
        skip = body.skipped.get(name)
        if skip:
            if skip.after != cursor:
                conflict(
                    "observation_cursor_conflict", "生产端跳过区间不匹配已确认游标"
                )
            cursor = skip.through
            gaps = state.get(name + "_gaps", [])
            state[name + "_gaps"] = [
                *gaps,
                {
                    "after": skip.after,
                    "through": skip.through,
                    "reason": "producer_retention",
                },
            ][-16:]
        for item in getattr(body, name):
            payload = item.model_dump(mode="json")
            if name == "logs":
                payload["text"] = redact_text(payload["text"], secrets)
                if len(payload["text"].encode()) > CHUNK_BYTES:
                    payload["text"] = "[log chunk omitted after redaction expansion]\n"
            else:
                total = payload["progress"].get("total_epochs")
                completed = payload["progress"]["completed_epoch"]
                if (
                    total is not None and total != run.config["parameters"]["epochs"]
                ) or completed > run.config["parameters"]["epochs"]:
                    raise HTTPException(
                        status_code=422, detail="观测轮次与冻结配置不符"
                    )
            digest = content_digest(payload)
            if item.sequence <= cursor:
                existing = await db.get(model, (attempt.id, item.sequence))
                if existing is None:
                    conflict("observation_cursor_retired", "该观测游标已被保留策略淘汰")
                if (
                    existing.content_digest != digest
                    or str(existing.stream_id) != stream_id
                ):
                    conflict("observation_content_conflict", "同一观测游标不能改写内容")
                continue
            if item.sequence != cursor + 1:
                conflict("observation_cursor_gap", "缺失观测必须显式声明生产端保留区间")
            db.add(
                model(
                    attempt_id=attempt.id,
                    sequence=item.sequence,
                    stream_id=body.stream_id,
                    producer_boot_id=item.producer_boot_id,
                    captured_at=item.captured_at,
                    content_digest=digest,
                    payload=payload,
                )
            )
            cursor = item.sequence
        state[name + "_cursor"] = cursor
        await db.flush()
        await db.execute(
            delete(model).where(
                model.attempt_id == attempt.id, model.sequence <= cursor - keep
            )
        )
    previous = state.get("summary", {})
    accepted = await db.scalar(
        select(TrainingTelemetrySample.payload)
        .where(TrainingTelemetrySample.attempt_id == attempt.id)
        .order_by(TrainingTelemetrySample.sequence.desc())
        .limit(1)
    )
    latest = previous.get("latest")
    if accepted and (not latest or accepted["sequence"] >= latest["sequence"]):
        latest = accepted
    peaks = dict(previous.get("peaks", {}))
    for sample in body.telemetry:
        for key, metric in sample.metrics.items():
            if metric.value is not None:
                peaks[key] = max(peaks.get(key, 0), metric.value)
    summary = {"protocol": 1, "latest": latest, "peaks": peaks}
    state["summary"] = summary
    cursors = {name + "_cursor": state[name + "_cursor"] for name in STREAMS}
    state["receipt"] = {
        "request_id": str(body.request_id),
        "digest": request_digest,
        "cursors": cursors,
    }
    if len(encoded(state)) > STATE_BYTES:
        raise HTTPException(status_code=422, detail="持久观测摘要超出上限")
    attempt.observation_state = state
    return {"stream_id": stream_id, "request_id": str(body.request_id), **cursors}


async def page(db, attempt, stream, *, after=None, before=None, limit=120):
    model, _ = STREAMS[stream]
    state = attempt.observation_state or {}
    latest = state.get(stream + "_cursor", 0)
    earliest = await db.scalar(
        select(func.min(model.sequence)).where(model.attempt_id == attempt.id)
    )
    statement = select(model).where(model.attempt_id == attempt.id)
    if after is not None:
        statement = statement.where(model.sequence > after).order_by(model.sequence)
    else:
        if before is not None:
            statement = statement.where(model.sequence < before)
        statement = statement.order_by(model.sequence.desc())
    rows = list((await db.scalars(statement.limit(limit))).all())
    if after is None:
        rows.reverse()
    next_cursor = (
        rows[-1].sequence
        if rows
        else max(after or 0, latest if earliest is None else 0)
    )
    gaps = list(state.get(stream + "_gaps", []))
    if (
        after is not None
        and (earliest is None or after + 1 < earliest)
        and after < latest
    ):
        through = earliest - 1 if earliest is not None else latest
        gaps = [*gaps, {"after": after, "through": through, "reason": "retention"}]
    if before is None:
        for gap in sorted(gaps, key=lambda item: item["after"]):
            if gap["after"] <= next_cursor < gap["through"]:
                next_cursor = gap["through"]
    result = {
        "attempt_id": attempt.id,
        "stream_id": state.get("stream_id"),
        "earliest_cursor": earliest,
        "latest_cursor": latest,
        "next_cursor": next_cursor,
        "has_more": next_cursor < latest,
        "gaps": gaps,
        "summary": state.get("summary"),
        "items": [{**row.payload, "received_at": row.received_at} for row in rows],
    }
    if stream == "telemetry":
        result["metric_definitions"] = METRICS
    return result


async def cleanup(db):
    cutoff = training.now() - timedelta(days=7)
    counts = {}
    for name, (model, _) in STREAMS.items():
        expired = (
            select(model.attempt_id, model.sequence)
            .where(model.received_at < cutoff)
            .order_by(model.received_at)
            .limit(2000)
        )
        result = await db.execute(
            delete(model).where(tuple_(model.attempt_id, model.sequence).in_(expired))
        )
        counts[name] = result.rowcount
    return counts


def redacted_log(run, attempt, name, artifact, *, transport=None):
    transport = storage if transport is None else transport
    owner = training.attempt_storage_owner(run, attempt, artifact["key"])
    expected = training.artifact_key(owner, attempt.id, name)
    if artifact["key"] != expected or artifact["size_bytes"] > LOG_BYTES:
        raise HTTPException(
            status_code=409, detail="历史日志超出脱敏下载上限或归属不符"
        )
    response = transport.client.get_object(Bucket=owner.bucket, Key=expected)
    digest = hashlib.sha256()
    total = 0
    redactor = RedactedLines((settings.training_executor_token,))
    output = bytearray()
    try:
        metadata = response.get("Metadata", {})
        if (
            response["ContentLength"] != artifact["size_bytes"]
            or metadata.get("attempt-id") != str(attempt.id)
            or metadata.get("sha256") != artifact["sha256"]
        ):
            raise ValueError("Historical log identity differs")
        for chunk in iter(lambda: response["Body"].read(64 * 1024), b""):
            total += len(chunk)
            if total > LOG_BYTES or total > artifact["size_bytes"]:
                raise ValueError("Historical log exceeds its bound")
            digest.update(chunk)
            for line in redactor.feed(chunk):
                output.extend(line.encode())
                if len(output) > LOG_BYTES:
                    raise ValueError("Redacted log exceeds its bound")
        for line in redactor.feed(b"", final=True):
            output.extend(line.encode())
        if (
            total != artifact["size_bytes"]
            or digest.hexdigest() != artifact["sha256"]
            or len(output) > LOG_BYTES
        ):
            raise ValueError("Historical log digest differs")
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="历史日志校验失败，不能生成脱敏下载"
        ) from exc
    finally:
        storage_access.close_registered(response["Body"])
    output_sha = hashlib.sha256(output).hexdigest()
    key = training.artifact_key(
        owner, attempt.id, f"redacted-logs/{name}.{output_sha}.log"
    )
    accounting = getattr(transport, "_write_accounting", None)
    observation = (
        write_accounting.Observation(
            accounting,
            transport.client,
            key,
            {"sha256": output_sha, "size_bytes": len(output)},
        )
        if accounting is not None
        else None
    )
    with observation.hooks() if observation is not None else nullcontext():
        transport.client.put_object(
            Bucket=owner.bucket,
            Key=key,
            Body=bytes(output),
            ContentType="text/plain; charset=utf-8",
            Metadata={
                "sha256": output_sha,
                "attempt-id": str(attempt.id),
                "source-sha256": artifact["sha256"],
                "redaction-revision": str(REDACTION_REVISION),
            },
        )
    if observation is not None:
        try:
            verified = transport.client.get_object(Bucket=owner.bucket, Key=key)
            with storage_access.closing_registered(verified["Body"]) as body:
                raw = body.read(len(output) + 1)
                if (
                    len(raw) != len(output)
                    or hashlib.sha256(raw).hexdigest() != output_sha
                ):
                    raise ValueError("Redacted output identity differs")
            observation.readback(verified)
        except Exception as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            # Read permission and observational uncertainty do not gate downloads.
        observation.complete()
    return {
        "key": key,
        "sha256": output_sha,
        "size_bytes": len(output),
        "source_sha256": artifact["sha256"],
        "redaction_revision": REDACTION_REVISION,
    }


def verify_summary(run, attempt, artifact, *, transport=None):
    transport = storage if transport is None else transport
    owner = training.attempt_storage_owner(run, attempt, artifact["key"])
    if artifact["size_bytes"] > SUMMARY_BYTES:
        raise ValueError("Observation artifact exceeds its bound")
    response = transport.client.get_object(Bucket=owner.bucket, Key=artifact["key"])
    try:
        body = json_load_bounded(response["Body"].read(SUMMARY_BYTES + 1))
    finally:
        storage_access.close_registered(response["Body"])
    if (
        body.get("protocol") != 1
        or body.get("run_id") != str(run.id)
        or body.get("attempt_id") != str(attempt.id)
        or body.get("config_digest") != run.config_digest
    ):
        raise ValueError("Observation artifact identity differs")


def json_load_bounded(raw):
    import json

    if len(raw) > SUMMARY_BYTES:
        raise ValueError("Observation artifact exceeds its bound")
    return json.loads(raw)


def checkpoint_available(run, attempt, checkpoint, *, transport=None):
    transport = storage if transport is None else transport
    try:
        owner = training.attempt_storage_owner(run, attempt, checkpoint["key"])
        response = transport.client.head_object(
            Bucket=owner.bucket, Key=checkpoint["key"]
        )
        metadata = response.get("Metadata", {})
        return (
            response["ContentLength"] == checkpoint["size_bytes"]
            and metadata.get("attempt-id") == str(attempt.id)
            and metadata.get("sha256") == checkpoint["sha256"]
        )
    except Exception:
        return False


async def recovery(db, run, *, check_storage=True, lease=None):
    attempt = (
        await db.get(TrainingAttempt, run.current_attempt_id)
        if run.current_attempt_id
        else None
    )
    if lease is None:
        lease = await db.get(TrainingResourceLease, run.config["resource_id"])
    online = bool(
        lease
        and lease.last_seen_at
        and lease.last_seen_at > training.now() - timedelta(seconds=30)
    )
    compatible = bool(lease and training.runtime_compatible(run.config, lease.runtime))
    reasons = []
    if (
        not attempt
        or run.status not in {"failed", "cancelled"}
        or attempt.status not in {"failed", "cancelled"}
    ):
        reasons.append("attempt_not_stopped")
    if lease and lease.attempt_id == run.current_attempt_id:
        reasons.append("lease_not_released")
    checkpoint = next(
        (
            item
            for item in (attempt.artifacts if attempt else {}).values()
            if item["role"] == "checkpoint"
        ),
        None,
    )
    epoch = checkpoint.get("epoch") if checkpoint else None
    if type(epoch) is not int or not 0 < epoch < run.config["parameters"]["epochs"]:
        reasons.append("unfinished_checkpoint_missing")
    if not lease or not lease.runtime:
        reasons.append("runtime_unknown")
    elif not compatible:
        reasons.append("runtime_incompatible")
    if run.study_id:
        from app.services import training_studies

        study = await training_studies.load(db, run.project_id, run.study_id)
        try:
            await training_studies.admit_resume(db, study, run)
        except HTTPException:
            reasons.append("study_budget_unavailable")
    try:
        await training.authority(db, run)
    except HTTPException:
        reasons.append("authority_unavailable")
    if checkpoint and check_storage:
        try:
            owner = training.attempt_storage_owner(run, attempt, checkpoint["key"])
            available = await storage_io.run(
                db,
                owner,
                "training_checkpoint_read",
                lambda transport: checkpoint_available(
                    run, attempt, checkpoint, transport=transport
                ),
            )
        except Exception:
            available = False
        if not available:
            reasons.append("checkpoint_unavailable")
    return {
        "attempt_id": attempt.id if attempt else None,
        "eligible": not reasons,
        "reasons": reasons,
        "executor_online": online,
        "runtime_compatible": compatible,
        "checkpoint_epoch": epoch if type(epoch) is int else None,
    }
