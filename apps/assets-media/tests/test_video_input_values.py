import json
from itertools import islice
from pathlib import Path
import subprocess
from uuid import UUID, uuid4

from pydantic import ValidationError
import pytest

from aap_assets_video_inputs import values as c


def video_input(**changes):
    identity = {
        "input_id": "00000000-0000-4000-8000-000000000001",
        "job_id": "00000000-0000-4000-8000-000000000002",
        "actor_id": "00000000-0000-4000-8000-000000000003",
        "task": {
            "task_id": "00000000-0000-4000-8000-000000000004",
            "project_id": "00000000-0000-4000-8000-000000000005",
            "task_version": 2,
            "task_source_generation": 3,
            "dataset_item_id": "00000000-0000-4000-8000-000000000006",
            "task_source_snapshot": "a" * 64,
            "file_path": "",
            "file_type": "video",
        },
        "source": {
            "dataset_item_id": "00000000-0000-4000-8000-000000000006",
            "dataset_id": "00000000-0000-4000-8000-000000000007",
            "source_generation": 4,
            "source_snapshot": "b" * 64,
            "timetable_revision": 5,
            "object_identity": {
                "etag": "origin",
                "byte_size": 4096,
                "version_id": None,
            },
        },
        "blob": {
            "key": "accepted-video-inputs/00000000-0000-4000-8000-000000000001/g1/"
            "a00000000-0000-4000-8000-000000000008/source",
            "version_id": "retained-1",
            "etag": "retained",
            "byte_size": 4096,
            "sha256": "c" * 64,
        },
        "video": {"frame_count": 17, "width": 1920, "height": 1080, "duration_ms": 800},
        "created_at": "2026-10-19T00:00:00Z",
    }
    identity.update(changes)
    return c.FrozenVideoInput.model_validate(identity)


def entries(video, sampling):
    for offset in range(sampling.count(video.video.frame_count)):
        yield c.FrameTimeEntry(
            ordinal=offset + 1,
            frame_index=offset * sampling.step,
            pts_ms=(-15, 22, 22, 141, 133)[offset % 5],
            is_keyframe=offset == 0,
        )


def plan():
    video = video_input()
    sampling = c.FrameSampling(max_frames=3, step=5)
    return c.FrozenFramePlan.from_entries(video, sampling, entries(video, sampling))


@pytest.mark.parametrize(
    "fault",
    [
        "link",
        "blob_scope",
        "copy_size",
        "empty_version",
        "null_version",
        "nul",
        "surrogate",
        "zero_frames",
        "bool_count",
        "unsafe_count",
        "second_stream",
    ],
)
def test_frozen_video_rejects_changed_or_unrepresentable_input(fault):
    raw = video_input().model_dump(mode="json")
    if fault == "link":
        raw["task"]["dataset_item_id"] = str(uuid4())
    elif fault == "blob_scope":
        raw["input_id"] = str(uuid4())
    elif fault == "copy_size":
        raw["blob"]["byte_size"] += 1
    elif fault in {"empty_version", "null_version"}:
        raw["blob"]["version_id"] = " " if fault == "empty_version" else "null"
    elif fault == "nul":
        raw["task"]["file_path"] = "bad\x00path"
    elif fault == "surrogate":
        raw["task"]["file_path"] = "bad\ud800path"
    elif fault == "zero_frames":
        raw["video"]["frame_count"] = 0
    elif fault == "bool_count":
        raw["video"]["frame_count"] = True
    elif fault == "unsafe_count":
        raw["video"]["frame_count"] = c.SAFE_INTEGER + 1
    else:
        raw["video"]["video_stream"] = 1
    with pytest.raises(ValidationError):
        c.FrozenVideoInput.model_validate(raw)


@pytest.mark.parametrize("fault", ["gap", "wrong_index", "missing", "extra"])
def test_timebase_requires_complete_selected_decoded_ordinals(fault):
    video = video_input()
    sampling = c.FrameSampling(max_frames=3, step=5)
    rows = [e.model_dump() for e in entries(video, sampling)]
    if fault == "gap":
        rows[1]["ordinal"] = 3
    elif fault == "wrong_index":
        rows[1]["frame_index"] += 1
    elif fault == "missing":
        rows.pop()
    else:
        rows.append({**rows[-1], "ordinal": 4, "frame_index": 15})
    with pytest.raises(ValueError):
        c.timebase_digest(video, sampling, (c.FrameTimeEntry(**r) for r in rows))


def test_sampling_caps_before_any_frame_table_allocation():
    video = video_input(
        video={
            "frame_count": c.SAFE_INTEGER,
            "width": 10,
            "height": 10,
            "duration_ms": 1,
        }
    )
    sampling = c.FrameSampling(max_frames=3, step=5)
    frozen = c.FrozenFramePlan.from_entries(video, sampling, entries(video, sampling))
    assert frozen.selected_count == 3
    assert [e.frame_index for e in entries(video, sampling)] == [0, 5, 10]


def test_unrepresentable_plan_is_rejected_before_consuming_frame_entries():
    video = video_input(
        video={
            "frame_count": c.SAFE_INTEGER,
            "width": 10,
            "height": 10,
            "duration_ms": 1,
        }
    )

    def unexpected_read():
        raise AssertionError("unrepresentable plan must not consume entries")
        yield

    with pytest.raises(ValueError):
        c.timebase_digest(
            video, c.FrameSampling(max_frames=3, step=2_147_483_647), unexpected_read()
        )


@pytest.mark.parametrize(
    "fault", [None, "missing", "repeat", "changed_pts", "oversized"]
)
def test_timebase_pages_are_bounded_and_verified_as_one_immutable_plan(fault):
    video = video_input(
        video={"frame_count": 205, "width": 10, "height": 10, "duration_ms": 5000}
    )
    sampling = c.FrameSampling(max_frames=900)
    frozen = c.FrozenFramePlan.from_entries(video, sampling, entries(video, sampling))

    def pages():
        source = entries(video, sampling)
        cursor = 0
        while rows := tuple(islice(source, 101 if fault == "oversized" else 100)):
            if fault == "missing" and cursor:
                return
            if fault == "changed_pts" and cursor:
                raw = rows[0].model_dump()
                raw["pts_ms"] += 1
                rows = (c.FrameTimeEntry(**raw), *rows[1:])
            page = c.FrameTimePage(
                plan=frozen,
                cursor=cursor,
                next_cursor=cursor + len(rows)
                if cursor + len(rows) < frozen.selected_count
                else None,
                entries=rows,
            )
            yield page
            if fault == "repeat":
                yield page
            cursor += len(rows)

    if fault is None:
        c.require_timebase(frozen, pages())
    else:
        with pytest.raises(ValueError):
            c.require_timebase(frozen, pages())


@pytest.mark.parametrize(
    "path",
    [
        ("source", "source_generation"),
        ("source", "timetable_revision"),
        ("task", "task_source_generation"),
        ("video", "frame_count"),
    ],
)
def test_plan_digest_fences_source_and_timebase_changes(path):
    raw = plan().model_dump(mode="json")
    raw["input"][path[0]][path[1]] += 1
    with pytest.raises(ValidationError):
        c.FrozenFramePlan.model_validate(raw)


def test_page_cursor_and_scope_are_independent_of_current_request_actor():
    frozen = plan()
    rows = list(entries(frozen.input, frozen.sampling))
    page = c.FrameTimePage(plan=frozen, cursor=0, next_cursor=2, entries=rows[:2])
    request = c.FramePageRequest(
        actor_id=uuid4(),
        job_id=frozen.input.job_id,
        task_id=frozen.input.task.task_id,
        input_id=frozen.input.input_id,
        plan_digest=frozen.plan_digest,
        page_size=2,
    )
    # This is attribution validation; current authorization belongs to the owner.
    c.require_page_scope(request, page)
    crossed = request.model_dump()
    crossed["job_id"] = uuid4()
    with pytest.raises(ValueError):
        c.require_page_scope(c.FramePageRequest(**crossed), page)
    with pytest.raises(ValidationError):
        c.FrameTimePage(plan=frozen, cursor=0, next_cursor=None, entries=rows[:2])


def frame_reference():
    frozen = plan()
    key = f"accepted-video-inputs/{frozen.input.input_id}/g2/a{UUID(int=9)}/frames/0/w1280-jpeg"
    return c.FrameReference(
        plan=frozen,
        entry=next(entries(frozen.input, frozen.sampling)),
        blob=c.FrameBlob(
            key=key,
            version_id="frame-version",
            etag="frame",
            byte_size=128,
            sha256="d" * 64,
        ),
        url=f"https://storage.invalid/retained/{key}?versionId=frame-version",
        expires_in=300,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "current_url",
        "wrong_version",
        "wrong_frame",
        "wrong_input",
        "wrong_rendition",
        "unselected",
        "other_key",
        "null_version",
        "blank_version",
        "zero_bytes",
    ],
)
def test_frame_reference_never_substitutes_a_current_or_unselected_frame(fault):
    raw = frame_reference().model_dump(mode="json")
    if fault == "current_url":
        raw["url"] = "https://storage.invalid/current-frame"
    elif fault == "wrong_version":
        raw["url"] += "&versionId=another"
    elif fault == "wrong_frame":
        raw["blob"]["key"] = raw["blob"]["key"].replace("/frames/0/", "/frames/1/")
    elif fault == "wrong_input":
        raw["blob"]["key"] = raw["blob"]["key"].replace(
            str(plan().input.input_id), str(uuid4())
        )
    elif fault == "wrong_rendition":
        raw["blob"]["key"] = raw["blob"]["key"].replace("w1280-jpeg", "w512-webp")
    elif fault == "other_key":
        raw["url"] = raw["url"].replace("/frames/0/", "/frames/99/")
    elif fault == "null_version":
        raw["blob"]["version_id"] = "null"
        raw["url"] = raw["url"].replace("versionId=frame-version", "versionId=null")
    elif fault == "blank_version":
        raw["blob"]["version_id"] = " "
        raw["url"] = raw["url"].replace("versionId=frame-version", "versionId=%20")
    elif fault == "zero_bytes":
        raw["blob"]["byte_size"] = 0
    else:
        raw["entry"].update(ordinal=4, frame_index=15)
    with pytest.raises(ValidationError):
        c.FrameReference.model_validate(raw)


@pytest.mark.parametrize("frame_count", [17, c.SAFE_INTEGER])
def test_node_consumes_values_and_recomputes_streamed_timebase_and_plan_digests(
    frame_count,
):
    video = video_input(
        video={
            "frame_count": frame_count,
            "width": 1920,
            "height": 1080,
            "duration_ms": 800,
        }
    )
    sampling = c.FrameSampling(max_frames=3, step=5)
    frozen = c.FrozenFramePlan.from_entries(video, sampling, entries(video, sampling))
    payload = {
        "plan": frozen.model_dump(mode="json"),
        "entries": [e.model_dump() for e in entries(frozen.input, frozen.sampling)],
    }
    fixture = Path(__file__).parent / "fixtures/video-input-values-peer.mjs"
    result = subprocess.run(
        ["rtk", "proxy", "node", str(fixture)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "selected_count": frozen.selected_count,
        "timebase_digest": frozen.timebase_digest,
        "plan_digest": frozen.plan_digest,
    }


def test_value_schema_is_packaged_and_current():
    artifact = Path(c.__file__).with_name("protocol.schema.json")
    assert json.loads(artifact.read_text()) == c.value_document()
