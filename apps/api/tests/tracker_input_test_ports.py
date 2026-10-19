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
    # Import before patching the service so the worker never retains a bound
    # method belonging to a previous test's closed session.
    from app.workers import video_tracker as worker

    class Ports:
        def __init__(self):
            self.status = None
            self.reference = None
            self.acceptance = None
            self.reference_calls = 0
            self.staged_evidence = []
            self.attached = []
            self.stage_lock_impl = None

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
                file_type="video",
                execution=execution,
                request_digest=_DIGEST,
                prompt_digest=_DIGEST,
                status="ready",
                plan_digest=_DIGEST,
            )
            self.source_version = SimpleNamespace(
                version_id="test-source-version",
                etag="test-source-etag",
                byte_size=1024,
                sha256=_SOURCE_SHA256,
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
            await self._load(tracker_job_id)
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
    monkeypatch.setattr(worker, "prepare_tracker_video_input", ports.prepare)
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
