"""Bind the portable window journal to the current Labeling transaction."""

from __future__ import annotations

import asyncio
from tempfile import TemporaryFile

from sqlalchemy import text

from aap_preannotation_results import contract as c
from aap_preannotation_results.video_execution_journal import (
    BeginWindowRequest,
    CaptureWindowRequest,
    CloseSpanRequest,
    FinishRequest,
    FreezePlanRequest,
    PutPartRequest,
    ReadPartRequest,
    ReadRequest,
    ResumePageRequest,
    SealWindowRequest,
    VerifyReplayRequest,
    VideoJournalFailure,
    decode_native_reply,
    request_bytes,
)
from aap_preannotation_results.video_execution_plan import VideoExecutionContractFailure
from aap_preannotation_results.video_window_checkpoint import (
    decide_video_window_resume,
    require_video_window_replay_match,
    snapshot_video_window_checkpoint,
    verify_video_window_checkpoint_parts,
)
from .execution import (
    TrackerExecutionBusy,
    TrackerExecutionCancelled,
    TrackerExecutionDeferred,
    TrackerExecutionLost,
    execution_proof,
)

_STEP = text("SELECT aap_jobs_v1.video_execution_checkpoint_step(:action,:arguments)")


def _translate(exc):
    if exc.code in {"lease_lost", "realm_mismatch", "job_missing", "job_not_active"}:
        return TrackerExecutionLost()
    if exc.code == "job_cancelled":
        return TrackerExecutionCancelled()
    if exc.code == "execution_busy":
        return TrackerExecutionBusy(exc.code)
    return TrackerExecutionDeferred(exc.code)


class TrackerWindowJournal:
    def __init__(self, db, job_id):
        self.db, self.job_id = db, job_id
        self.plan = None
        self.manifests = {}
        self.reuse_ordinals = frozenset()

    def request(self, model, **fields):
        return model(**execution_proof(self.job_id).model_dump(), **fields)

    async def step(self, action, request):
        """No commit: terminal ownership handoff shares the business transaction."""
        try:
            with self.db.no_autoflush:
                raw = await self.db.scalar(
                    _STEP,
                    {
                        "action": action,
                        "arguments": request_bytes(action, request).decode(),
                    },
                )
            return decode_native_reply(action, request, raw)
        except VideoJournalFailure as exc:
            raise _translate(exc) from None
        except (
            TrackerExecutionDeferred,
            TrackerExecutionLost,
            TrackerExecutionCancelled,
        ):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TrackerExecutionDeferred("storage_unavailable") from None

    async def committed(self, action, request):
        """Retry persistence with the held bytes and the same immutable identity."""
        storage_failures = 0
        while True:
            try:
                result = await self.step(action, request)
                await self.db.commit()
                return result
            except TrackerExecutionBusy:
                await self.db.rollback()
                await asyncio.sleep(0.2)
            except TrackerExecutionDeferred as exc:
                await self.db.rollback()
                if str(exc) != "storage_unavailable" or storage_failures >= 2:
                    raise
                storage_failures += 1
                await asyncio.sleep(0.2)
            except (TrackerExecutionLost, TrackerExecutionCancelled):
                await self.db.rollback()
                raise
            except asyncio.CancelledError:
                await self.db.rollback()
                raise
            except Exception:
                # A commit may have succeeded. Repeating the exact request is
                # idempotent; inference is never retried inside this loop.
                await self.db.rollback()
                if storage_failures >= 2:
                    raise TrackerExecutionDeferred("storage_unavailable") from None
                storage_failures += 1
                await asyncio.sleep(0.2)

    async def read_plan(self):
        self.plan = (await self.committed("read_plan", self.request(ReadRequest)))[
            "plan"
        ]
        return self.plan

    async def input_facts(self):
        return (await self.committed("input_facts", self.request(ReadRequest)))["facts"]

    async def freeze(self, plan):
        self.plan = (
            await self.committed(
                "freeze_plan", self.request(FreezePlanRequest, plan=plan)
            )
        )["plan"]
        return self.plan

    async def resume(self):
        if self.plan is None:
            raise TrackerExecutionDeferred("plan_not_frozen")
        windows, closures, cursor = [], {}, -1
        while True:
            page = (
                await self.committed(
                    "read_resume", self.request(ResumePageRequest, after_ordinal=cursor)
                )
            )["page"]
            for slot in page.windows:
                if slot.ordinal != len(windows) or slot.ordinal >= len(
                    self.plan.windows
                ):
                    raise TrackerExecutionDeferred("invalid_response")
                expected = self.plan.windows[slot.ordinal]
                if (
                    any(
                        getattr(slot, key) != getattr(expected, key)
                        for key in (
                            "ordinal",
                            "window_digest",
                            "phase",
                            "branch",
                            "seed_source",
                            "seed_digest",
                        )
                    )
                    or slot.context_span_digest != expected.context_span.span_digest
                ):
                    raise TrackerExecutionDeferred("invalid_response")
                windows.append(slot)
            relevant = {slot.context_span_digest for slot in page.windows}
            for closure in page.closed_spans:
                if closure.context_span_digest not in relevant or (
                    closure.context_span_digest in closures
                    and closures[closure.context_span_digest] != closure.closed
                ):
                    raise TrackerExecutionDeferred("invalid_response")
                closures[closure.context_span_digest] = closure.closed
            if page.next_ordinal is None:
                break
            cursor = page.next_ordinal
        if len(windows) != len(self.plan.windows):
            raise TrackerExecutionDeferred("invalid_response")
        manifests = [slot.checkpoint for slot in windows if slot.checkpoint is not None]
        try:
            decision = decide_video_window_resume(
                self.plan,
                manifests,
                closed_context_span_digests=[
                    key for key, value in closures.items() if value
                ],
            )
        except VideoExecutionContractFailure as exc:
            raise TrackerExecutionDeferred(exc.code) from None
        self.manifests = {item.window_ordinal: item for item in manifests}
        self.reuse_ordinals = frozenset(decision.reuse_ordinals)
        return decision

    def predecessor(self, window):
        if window.seed_source in {"none", "prompt", "source_geometry", "correction"}:
            return None
        return self.manifests.get(window.ordinal - 1)

    async def begin(self, window):
        previous = self.predecessor(window)
        seed = window.seed_digest
        if window.seed_source == "discovery":
            seed = previous.discovery_seeds_digest if previous else None
        elif window.seed_source == "continuation":
            seed = previous.continuation_digest if previous else None
        return (
            await self.committed(
                "begin_window",
                self.request(
                    BeginWindowRequest,
                    plan_digest=self.plan.plan_digest,
                    window_ordinal=window.ordinal,
                    window_digest=window.window_digest,
                    seed_input_digest=seed,
                    predecessor_checkpoint_digest=previous.manifest_digest
                    if previous
                    else None,
                ),
            )
        )["attempt"]

    async def load(self, window):
        manifest = self.manifests[window.ordinal]
        # Spool Base64 transport parts to disk; never retain a second full
        # encoded copy alongside the bounded decoded window payload.
        with TemporaryFile(mode="w+b") as spool:
            for index in range(manifest.payload_part_count):
                part = (
                    await self.committed(
                        "read_part",
                        self.request(
                            ReadPartRequest,
                            checkpoint_id=manifest.checkpoint_id,
                            index=index,
                        ),
                    )
                )["part"]
                spool.write(c.json_bytes(part) + b"\n")
            spool.seek(0)
            try:
                with verify_video_window_checkpoint_parts(
                    self.plan,
                    manifest,
                    (c.parse_json(line) for line in spool),
                    predecessor_checkpoint=self.predecessor(window),
                ) as payload:
                    return payload
            except VideoExecutionContractFailure as exc:
                raise TrackerExecutionDeferred(exc.code) from None

    async def save(self, window, attempt, payload):
        predecessor = self.predecessor(window)
        try:
            with snapshot_video_window_checkpoint(
                self.plan, payload, predecessor_checkpoint=predecessor
            ) as snapshot:
                manifest = snapshot.manifest
                if attempt.mode == "replay":
                    stored = await self.load(window)
                    require_video_window_replay_match(
                        plan=self.plan,
                        stored_manifest=attempt.checkpoint,
                        stored_payload=stored,
                        replayed_payload=payload,
                        predecessor_checkpoint=predecessor,
                    )
                    await self.committed(
                        "verify_replay",
                        self.request(
                            VerifyReplayRequest,
                            plan_digest=self.plan.plan_digest,
                            window_ordinal=window.ordinal,
                            capture_id=attempt.capture_id,
                            checkpoint_id=manifest.checkpoint_id,
                            result_digest=manifest.result_digest,
                            continuation_digest=manifest.continuation_digest,
                            discovery_seeds_digest=manifest.discovery_seeds_digest,
                            seed_input_digest=manifest.seed_input_digest,
                            predecessor_checkpoint_digest=manifest.predecessor_checkpoint_digest,
                            raw_response_digest=manifest.raw_response_digest,
                        ),
                    )
                    return stored
                await self.committed(
                    "capture_window",
                    self.request(
                        CaptureWindowRequest,
                        plan_digest=self.plan.plan_digest,
                        window_ordinal=window.ordinal,
                        capture_id=attempt.capture_id,
                        manifest=manifest,
                    ),
                )
                for part in snapshot.iter_parts():
                    receipt = (
                        await self.committed(
                            "put_part",
                            self.request(
                                PutPartRequest,
                                capture_id=attempt.capture_id,
                                index=part.part_index,
                                sha256=part.part_digest,
                                data_base64=part.data_base64,
                            ),
                        )
                    )["part"]
                    if any(
                        getattr(receipt, key) != getattr(part, key)
                        for key in (
                            "checkpoint_id",
                            "manifest_digest",
                            "part_index",
                            "part_count",
                            "payload_digest",
                            "payload_byte_size",
                            "part_digest",
                        )
                    ):
                        raise TrackerExecutionDeferred("invalid_response")
                sealed = (
                    await self.committed(
                        "seal_window",
                        self.request(SealWindowRequest, capture_id=attempt.capture_id),
                    )
                )["window"]
                if sealed.manifest != manifest:
                    raise TrackerExecutionDeferred("invalid_response")
                self.manifests[window.ordinal] = manifest
                return payload
        except VideoExecutionContractFailure as exc:
            raise TrackerExecutionDeferred(exc.code) from None

    async def close_span(self, span_digest):
        await self.committed(
            "close_span",
            self.request(
                CloseSpanRequest,
                plan_digest=self.plan.plan_digest,
                context_span_digest=span_digest,
            ),
        )

    async def finish(self, outcome):
        await self.db.flush()
        await self.step("finish", self.request(FinishRequest, outcome=outcome))

    async def retire(self):
        await self.committed("retire", self.request(ReadRequest))
