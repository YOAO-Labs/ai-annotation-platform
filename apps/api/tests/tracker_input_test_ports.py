"""Explicit owner-port fakes for tracker runner/review unit tests.

These fakes supply an already captured and sealed input to legacy ORM-focused
tests. Live owner behavior is exercised by the dedicated tracker input test.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

from aap_assets_video_inputs import tracker as t
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services import accepted_tracker_inputs


_DIGEST = "a" * 64
_SOURCE_SHA256 = "b" * 64


async def yield_tracker_fake_results(ctx, results):
    """Yield direct fake frames and record the exact fake response observation."""
    rows = list(results)
    for result in rows:
        yield result
    if ctx.response_digest is not None:
        from app.services.video_tracking.adapters import _response_digest

        ctx.response_digest.append(
            _response_digest(
                {
                    "result": [
                        {
                            "frame_index": result.frame_index,
                            "geometry": result.geometry,
                            "confidence": result.confidence,
                            "outside": result.outside,
                            "instance_id": result.instance_id,
                            "primary": result.primary,
                        }
                        for result in rows
                    ],
                    "meta": {},
                }
            )
        )


def tracker_input_evidence_for(job, task):
    return t.TrackerVideoInputEvidence(
        tracker_job_id=job.id,
        input_id=uuid.uuid5(uuid.NAMESPACE_URL, f"tracker-input:{job.id}"),
        task_id=task.id,
        project_id=task.project_id,
        request_digest=_DIGEST,
        prompt_digest=_DIGEST,
        plan_digest=_DIGEST,
        timebase_digest=_DIGEST,
        source_version_id="test-source-version",
        source_sha256=_SOURCE_SHA256,
        from_frame=job.from_frame,
        to_frame=job.to_frame,
        direction=job.direction,
        grid_step=1,
    )


def seed_staged_tracker_input(job, task, source_instance_map=None):
    staged = dict(job.staged_result or {})
    staged["tracker_input_evidence"] = tracker_input_evidence_for(job, task).model_dump(
        mode="json", by_alias=True
    )
    staged["source_instance_map"] = {
        str(instance_id): str(annotation_id)
        for instance_id, annotation_id in (source_instance_map or {}).items()
    }
    job.staged_result = staged


def create_tracker_input_owner_fakes(db_session, monkeypatch):
    from sqlalchemy import text
    from app.services.video_tracking import recovery, runner
    from tests.test_video_execution_native import _seed_claimable_input

    claimed_execution = runner.tracker_execution

    @asynccontextmanager
    async def claim_captured_unit_input(db, job_id, **kwargs):
        # These unit ports declare the input already accepted. Supply its
        # minimal Native claim scope; full input admission is tested separately.
        present = await db.scalar(
            text(
                "SELECT 1 FROM public.video_tracker_input_scopes WHERE tracker_job_id=:job"
            ),
            {"job": job_id},
        )
        if present is None:
            job = await db.get(VideoTrackerJob, job_id)
            task = await db.get(Task, job.task_id)
            actor_id = job.created_by
            if actor_id is None:
                actor_id = await db.scalar(
                    text("SELECT owner_id FROM public.projects WHERE id=:project"),
                    {"project": task.project_id},
                )
            await _seed_claimable_input(
                db, actor_id, task.project_id, task.dataset_item_id, task.id, job_id
            )
            await db.commit()
        async with claimed_execution(db, job_id, **kwargs) as claim:
            yield claim

    monkeypatch.setattr(runner, "tracker_execution", claim_captured_unit_input)

    class Ports:
        def __init__(self):
            self.status = None
            self.reference = None
            self.acceptance = None
            self.reference_calls = 0
            self.staged_evidence = []
            self.attached = []
            self.stage_lock_impl = None
            self.declare_tracker_backend = False

        async def _load(self, tracker_job_id):
            job = await db_session.get(VideoTrackerJob, tracker_job_id)
            if job is None:
                raise AssertionError("tracker unit test Job disappeared")
            task = await db_session.get(Task, job.task_id)
            if task is None:
                raise AssertionError("tracker unit test Task disappeared")
            item = await db_session.get(DatasetItem, task.dataset_item_id)
            video = ((item.metadata_ or {}).get("video") or {}) if item else {}
            frozen_video = SimpleNamespace(
                frame_count=video.get("frame_count", 90),
                width=(item.width if item else None) or video.get("width", 3),
                height=(item.height if item else None) or video.get("height", 2),
                duration_ms=video.get("duration_ms", 3000),
                selected_frame_count=video.get(
                    "selected_frame_count", video.get("frame_count", 90)
                ),
                sampling_step=video.get("sampling_step", 1),
                video_stream=video.get("video_stream", video.get("stream_index", 0)),
            )
            prompt = dict(job.prompt or {})
            prompt_snapshot = {
                key: value
                for key, value in prompt.items()
                if key
                not in {
                    "expected_source_versions",
                    "review_state",
                    "touched_annotation_ids",
                }
            }
            source_ids = set()
            if job.annotation_id is not None:
                source_ids.add(job.annotation_id)
            for seed in prompt_snapshot.get("seeds") or []:
                if isinstance(seed, dict) and seed.get("source_annotation_id"):
                    try:
                        source_ids.add(uuid.UUID(str(seed["source_annotation_id"])))
                    except (TypeError, ValueError):
                        continue
            sources = []
            for annotation_id in sorted(source_ids, key=str):
                annotation = await db_session.get(Annotation, annotation_id)
                if annotation is not None:
                    sources.append(
                        SimpleNamespace(
                            annotation_id=annotation.id,
                            version=int(annotation.version or 1),
                            geometry=annotation.geometry or {},
                        )
                    )
            correction_mask = None
            correction = prompt_snapshot.get("correction") or {}
            if correction and sources:
                annotation = await db_session.get(Annotation, sources[0].annotation_id)
                geometry = annotation.geometry or {} if annotation is not None else {}
                frame = int(
                    correction.get("correction_frame", job.correction_frame or 0)
                )
                keyframe = next(
                    (
                        row
                        for row in geometry.get("keyframes") or []
                        if int(row.get("frame_index", -1)) == frame
                    ),
                    {},
                )
                correction_mask = SimpleNamespace(
                    annotation_id=sources[0].annotation_id,
                    annotation_version=sources[0].version,
                    frame_index=frame,
                    mask=keyframe.get("mask") or {},
                    mask_digest=str(correction.get("corrected_digest") or _DIGEST),
                )
            execution = SimpleNamespace(
                job_kind=job.job_kind,
                model_key=job.model_key,
                direction=job.direction,
                from_frame=job.from_frame,
                to_frame=job.to_frame,
                grid_step=1,
                correction_frame=job.correction_frame,
                segment_id=job.segment_id,
                track_id_snapshot=job.track_id_snapshot,
                target_class_name=job.target_class_name,
                target_tool_unit_id=job.target_tool_unit_id,
                prompt_snapshot=prompt_snapshot,
                source_annotations=tuple(sources),
                correction_mask=correction_mask,
            )
            input_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tracker-input:{job.id}")
            self.status = SimpleNamespace(
                tracker_job_id=job.id,
                input_id=input_id,
                origin_actor_id=job.created_by,
                task_id=task.id,
                project_id=task.project_id,
                source_item_id=task.dataset_item_id,
                file_type="video",
                execution=execution,
                request_digest=_DIGEST,
                prompt_digest=_DIGEST,
                timebase_digest=_DIGEST,
                selected_frame_count=frozen_video.selected_frame_count,
                sampling_step=frozen_video.sampling_step,
                status="ready",
                plan_digest=_DIGEST,
            )
            self.source_version = SimpleNamespace(
                version_id="test-source-version",
                etag="test-source-etag",
                byte_size=1024,
                sha256=_SOURCE_SHA256,
                video=SimpleNamespace(
                    frame_count=frozen_video.frame_count,
                    width=frozen_video.width,
                    height=frozen_video.height,
                    duration_ms=frozen_video.duration_ms,
                ),
                video_stream=frozen_video.video_stream,
            )
            self.reference = SimpleNamespace(
                input=SimpleNamespace(
                    tracker_job_id=job.id,
                    task_id=task.id,
                    input_id=input_id,
                    request_digest=_DIGEST,
                    prompt_digest=_DIGEST,
                    plan_digest=_DIGEST,
                    execution=execution,
                    status="ready",
                ),
                source_version=self.source_version,
                url=f"https://source.test/{job.id}?version=test-source-version",
                expires_in=900,
            )
            evidence = tracker_input_evidence_for(job, task)
            scope = SimpleNamespace(
                tracker_job_id=job.id,
                origin_actor_id=job.created_by,
                task=SimpleNamespace(task_id=task.id),
                execution=execution,
                request_digest=_DIGEST,
                prompt_digest=_DIGEST,
            )
            self.acceptance = SimpleNamespace(
                input=SimpleNamespace(
                    scope=scope,
                    plan=SimpleNamespace(input=SimpleNamespace(video=frozen_video)),
                ),
                evidence=evidence,
            )
            return job

        async def prepare(self, *, tracker_job_id, **_kwargs):
            job = await self._load(tracker_job_id)
            if self.declare_tracker_backend and job.model_key != "mock_bbox":
                from app.db.models.project import Project
                from app.db.models.ml_backend_registry import ProjectMLBackendPool
                from tests.conftest import create_registry_with_pool

                project = await db_session.get(Project, self.status.project_id)
                if project.ml_backend_pool_id is None:
                    keys = (
                        ["sam3_video", "sam3_video_interactive"]
                        if job.model_key == "sam3_video_combo"
                        else [job.model_key]
                    )
                    # Direct fake adapters declare a test registry, without
                    # claiming a weight artifact or real backend qualification.
                    _, pool = await create_registry_with_pool(
                        db_session,
                        name="tracker unit provider",
                        url="http://tracker-unit.test",
                        state="connected",
                        enabled_pool=True,
                        is_interactive=True,
                        extra_params={"device": "cpu", "max_concurrency": 8},
                        health_meta={
                            "capabilities": {
                                "supported_trackers": keys,
                                "models": [
                                    {
                                        "id": key,
                                        "supported_trackers": [key],
                                        "supported_geometric_outputs": [
                                            "bbox",
                                            "polygon",
                                            "mask",
                                        ],
                                    }
                                    for key in keys
                                ],
                            }
                        },
                    )
                    project.ml_backend_pool_id = pool.id
                    db_session.add(
                        ProjectMLBackendPool(
                            project_id=project.id, pool_id=pool.id, enabled=True
                        )
                    )
                    await db_session.flush()
            return SimpleNamespace(status=self.status, reference=self.reference)

        async def lock_stage(self, _db, request):
            await self._load(request.tracker_job_id)
            if self.stage_lock_impl is not None:
                return await self.stage_lock_impl()
            return self.acceptance

        async def lock_accept(self, _db, request):
            await self._load(request.tracker_job_id)
            return self.acceptance

        async def attach(
            self, _db, *, actor_id, tracker_job_id, annotation_id, evidence
        ):
            self.attached.append((actor_id, tracker_job_id, annotation_id, evidence))
            return SimpleNamespace(attached=True)

    ports = Ports()

    class InMemoryTrackerWindowJournal:
        """Typed first-execution journal for ORM-focused runner unit tests.

        This intentionally models no Native checkpoint storage or physical
        scope retirement. Dedicated owner/role tests cover those contracts.
        """

        def __init__(self, db, tracker_job_id):
            self.db = db
            self.tracker_job_id = tracker_job_id
            self.status = ports.status
            self.source_version = getattr(ports, "source_version", None)
            self.plan = None
            self.manifests = {}
            self.payloads = {}
            self.closed_spans = set()
            self.reuse_ordinals = frozenset()

        async def read_plan(self):
            return None

        async def input_facts(self):
            from aap_preannotation_results.video_execution_planner import (
                VideoExecutionInputFacts,
            )

            status = self.status
            execution = status.execution
            source = self.source_version
            video = source.video
            return VideoExecutionInputFacts(
                job_id=self.tracker_job_id,
                input_id=status.input_id,
                task_id=status.task_id,
                project_id=status.project_id,
                source_item_id=status.source_item_id,
                request_digest=status.request_digest,
                prompt_digest=status.prompt_digest,
                source_plan_digest=status.plan_digest,
                requested_model_key=execution.model_key,
                job_kind=execution.job_kind,
                direction=execution.direction,
                from_frame=execution.from_frame,
                to_frame=execution.to_frame,
                grid_step=execution.grid_step,
                correction_frame=execution.correction_frame,
                segment_id=execution.segment_id,
                source={
                    "source_version_id": source.version_id,
                    "source_sha256": source.sha256,
                    "byte_size": source.byte_size,
                    "frame_count": video.frame_count,
                    "width": video.width,
                    "height": video.height,
                    "duration_ms": video.duration_ms,
                    "video_stream": source.video_stream,
                },
                timebase={
                    "timebase_digest": status.timebase_digest,
                    "selected_frame_count": status.selected_frame_count,
                    "sampling_step": status.sampling_step,
                    "grid_step": execution.grid_step,
                },
            )

        async def freeze(self, plan):
            from aap_preannotation_results.video_execution_plan import (
                decode_video_execution_plan,
            )

            self.plan = decode_video_execution_plan(plan)
            return self.plan

        async def resume(self):
            from aap_preannotation_results.video_window_checkpoint import (
                decide_video_window_resume,
            )

            if self.plan is None:
                raise AssertionError("unit journal resumed before plan freeze")
            decision = decide_video_window_resume(
                self.plan,
                (),
                closed_context_span_digests=(),
            )
            self.reuse_ordinals = frozenset(decision.reuse_ordinals)
            return decision

        def predecessor(self, window):
            if window.seed_source in {
                "none",
                "prompt",
                "source_geometry",
                "correction",
            }:
                return None
            return self.manifests.get(window.ordinal - 1)

        async def begin(self, window):
            if self.plan is None or window.ordinal >= len(self.plan.windows):
                raise AssertionError("unit journal began an unfrozen window")
            return SimpleNamespace(
                capture_id=uuid.uuid4(),
                mode="execute",
                checkpoint=None,
            )

        async def load(self, window):
            try:
                return self.payloads[window.ordinal]
            except KeyError as exc:
                raise AssertionError("unit journal has no held window payload") from exc

        async def save(self, window, attempt, payload):
            from aap_preannotation_results.video_window_checkpoint import (
                decode_video_window_checkpoint_payload,
                snapshot_video_window_checkpoint,
            )

            if attempt.mode != "execute" or attempt.checkpoint is not None:
                raise AssertionError("unit journal only accepts first executions")
            typed_payload = decode_video_window_checkpoint_payload(payload)
            with snapshot_video_window_checkpoint(
                self.plan,
                typed_payload,
                predecessor_checkpoint=self.predecessor(window),
            ) as snapshot:
                self.manifests[window.ordinal] = snapshot.manifest
                self.payloads[window.ordinal] = typed_payload
            return typed_payload

        async def close_span(self, span_digest):
            span = next(
                (
                    window.context_span
                    for window in self.plan.windows
                    if window.context_span.span_digest == span_digest
                ),
                None,
            )
            if span is None or any(
                ordinal not in self.manifests
                for ordinal in range(span.start_ordinal, span.end_ordinal + 1)
            ):
                raise AssertionError("unit journal closed an incomplete context span")
            self.closed_spans.add(span_digest)

        async def finish(self, _outcome):
            # Finish participates in the business transaction; only flush here.
            await self.db.flush()

        async def retire(self):
            # ORM-focused runner tests do not qualify physical Native retirement.
            return None

    monkeypatch.setattr(runner, "TrackerWindowJournal", InMemoryTrackerWindowJournal)
    monkeypatch.setattr(recovery, "TrackerWindowJournal", InMemoryTrackerWindowJournal)

    async def _unit_scope_retirement(_db, _job_id, **_kwargs):
        # Native retirement is covered by its owner tests, not this test port.
        return None

    monkeypatch.setattr(recovery, "retire_tracker_scope", _unit_scope_retirement)

    @asynccontextmanager
    async def gateway(**_kwargs):
        class FakeGateway:
            async def reference(self, _request):
                ports.reference_calls += 1
                return ports.reference

        yield FakeGateway()

    async def attach_source(_db, *, actor_id, tracker_job_id, annotation_id, evidence):
        return await ports.attach(
            _db,
            actor_id=actor_id,
            tracker_job_id=tracker_job_id,
            annotation_id=annotation_id,
            evidence=evidence,
        )

    monkeypatch.setattr(
        accepted_tracker_inputs, "prepare_tracker_video_input", ports.prepare
    )
    monkeypatch.setattr(accepted_tracker_inputs, "tracker_video_input_gateway", gateway)
    monkeypatch.setattr(
        accepted_tracker_inputs, "lock_video_tracker_input_for_stage", ports.lock_stage
    )
    monkeypatch.setattr(
        accepted_tracker_inputs,
        "lock_video_tracker_input_for_accept",
        ports.lock_accept,
    )
    monkeypatch.setattr(
        accepted_tracker_inputs,
        "attach_annotation_video_tracker_input_source",
        attach_source,
    )

    return ports
