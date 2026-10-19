"""Original-video facts shared by owner execution regressions."""

from uuid import UUID
from pathlib import Path
import shutil
import subprocess

import pytest

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from test_video_input_values import entries, plan

CURRENT_ACTOR = UUID("00000000-0000-4000-8000-000000000099")
WORKER = UUID("00000000-0000-4000-8000-000000000098")
LEASE = UUID("00000000-0000-4000-8000-000000000008")
FRAME_LEASE = UUID("00000000-0000-4000-8000-000000000009")
FRAME_ID = UUID("00000000-0000-4000-8000-000000000010")


def record(status="pending"):
    frozen = plan()
    video = frozen.input
    return c.VideoInputRecord(
        input_id=video.input_id,
        job_id=video.job_id,
        actor_id=video.actor_id,
        task=video.task,
        source=video.source,
        sampling=frozen.sampling,
        status=status,
        plan=frozen if status in {"ready", "expired"} else None,
        error="processing_failed" if status == "failed" else None,
        created_at=video.created_at,
        retire_after=None,
    )


def source(*, observed=True):
    accepted = record()
    return c.VideoCaptureSource(
        **accepted.source.model_dump(exclude={"object_identity"}),
        object_identity=accepted.source.object_identity if observed else None,
        source_drifted=False,
        file_path="dataset/original.mp4",
        file_type="video",
        file_size=4096,
        content_hash=None,
    )


def manifest(*, sealed=False, selected=1):
    accepted = record()
    return c.VideoManifestHeader(
        job_id=accepted.job_id,
        actor_id=accepted.actor_id,
        project_id=accepted.task.project_id,
        selection=c.VideoSelection(
            batch_id=None,
            task_ids=(accepted.task.task_id,),
            predict_mode="overwrite",
            execution_scope="bulk",
            sampling=accepted.sampling,
        ),
        selection_digest="d" * 64,
        candidate_count=selected,
        selected_count=selected,
        prepared_count=selected if sealed else 0,
        status="sealed" if sealed else "preparing",
        ready_count=selected if sealed else None,
        failed_count=0 if sealed else None,
        sealed_digest="e" * 64 if sealed else None,
        created_at=accepted.created_at,
        sealed_at=accepted.created_at if sealed else None,
    )


def member(state="unprepared"):
    accepted = record("ready" if state == "ready" else "pending")
    return c.VideoManifestMember(
        ordinal=1,
        task=c.CapturedVideoTask(**accepted.task.model_dump(), batch_id=None),
        source=source(observed=False),
        state=state,
        input=accepted if state in {"preparing", "ready"} else None,
        error="source_missing" if state == "failed" else None,
    )


def claim():
    return c.VideoInputClaim(
        input=record("materializing"),
        source=source(),
        generation=1,
        lease_token=LEASE,
        lease_expires_at="2026-10-19T01:00:00Z",
        blob_key=plan().input.blob.key,
    )


def time_entries():
    frozen = plan()
    return tuple(entries(frozen.input, frozen.sampling))


def frame(status="pending"):
    frozen = plan()
    return c.VideoFrameRecord(
        frame_id=FRAME_ID,
        plan=frozen,
        entry=time_entries()[0],
        status=status,
        blob=v.FrameBlob(
            key=c.attempt_prefix(frozen.input.input_id, 2, FRAME_LEASE)
            + "frames/0/w1280-jpeg",
            version_id="frame-version-2",
            etag="frame",
            byte_size=111,
            sha256="f" * 64,
        )
        if status in {"ready", "expired"}
        else None,
        error="processing_failed" if status == "failed" else None,
        created_at=frozen.input.created_at,
    )


def write_video(path: Path) -> Path:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("real retained-video qualification requires ffmpeg and ffprobe")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=0.8",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path
