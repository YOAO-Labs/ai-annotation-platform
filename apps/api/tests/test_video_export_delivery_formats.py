"""Real detached serializers, integer budgets, CPU cancellation and fetch receipts."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import uuid
import zipfile

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting import packaging, video_budget
from app.services.exporting.video_frozen import FrozenVideo
from app.services.exporting.video_media_paths import paths, sequence_paths
from tests.test_video_export_frozen import facts


RAW = b"video7!"
SHA = hashlib.sha256(RAW).hexdigest()


class Context(FrozenVideo):
    """Production frozen view with preverified copies; no live business session."""

    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.paths, self.sequence_paths = paths(snapshot), sequence_paths(snapshot)
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self.format_options = deepcopy(snapshot["options"])
        for task in self.tasks:
            info = {
                "fps": 30,
                "frame_count": 10,
                "width": 4,
                "height": 3,
                "codec": "h264",
                "duration_ms": 333,
            }
            self.metadata_by_task[task.id] = info
            self.metadata_receipts[task.id] = {
                "probe_contract": 1,
                "video": deepcopy(info),
                "sha256": SHA,
                "size_bytes": len(RAW),
            }

    def service(self, db):
        return super().service(
            db,
            media_paths={key: "videos/" + value for key, value in self.paths.items()},
        )

    def entry(self, task_id):
        return {
            "presigned_url": "https://saved.invalid/object?signature=private",
            "expires_at": self.expires_at.isoformat(),
            "sha256": SHA,
            "size_bytes": len(RAW),
        }


def context(*, targets=None, original=False, mask=False, same_name=False):
    data = facts(original=original)
    data["targets"] = targets or [
        "video_json",
        "aap_json",
        "mot",
        "kitti",
        "yolo-frames-det",
        "yolo-frames-seg",
        "coco-frames-seg",
        "davis",
        "youtube-vos",
        "mots",
    ]
    data["options"] = {"video_frame_mode": "all_frames", "include_attributes": True}
    data["media"][0]["source"].update(sha256=SHA, size_bytes=len(RAW))
    polygon = deepcopy(data["annotations"][0])
    polygon["id"] = str(uuid.uuid4())
    polygon["geometry"] = {
        "type": "video_track_polygon",
        "track_id": "polygon",
        "keyframes": [
            {"frame_index": frame, "points": [[0, 0], [0.5, 0], [0.5, 0.5]]}
            for frame in (0, 9)
        ],
        "outside": [],
    }
    data["annotations"].append(polygon)
    if mask:
        annotation = deepcopy(data["annotations"][0])
        annotation["id"] = str(uuid.uuid4())
        reference = {
            "encoding": "coco_rle_ref",
            "object_key": "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json",
            "sha256": "a" * 64,
            "runs": 3,
            "bytes": 40,
            "size": [3, 4],
        }
        annotation["geometry"] = {
            "type": "video_track_mask",
            "track_id": "mask",
            "keyframes": [
                {"frame_index": frame, "mask": deepcopy(reference)} for frame in (1, 7)
            ],
            "outside": [],
        }
        data["annotations"].append(annotation)
    if same_name:
        other = deepcopy(data["tasks"][0])
        other.update(
            id=str(uuid.uuid4()),
            display_id="T2",
            file_path="saved/other-original",
            original_id=str(uuid.uuid4()),
        )
        data["tasks"].append(other)
        source = deepcopy(data["media"][0])
        source.update(task_id=other["id"], source_key="c" * 64)
        source["source"]["object_key"] = other["file_path"]
        data["media"].append(source)
    return Context(data)


def forbidden(*args, **kwargs):
    raise AssertionError(
        "frozen video serialization read live rows or signed an original"
    )


async def archive(context, monkeypatch, tmp_path, *, check_cancel=None):
    path = tmp_path / (uuid.uuid4().hex + ".zip")
    monkeypatch.setattr(packaging, "_new_zip_tempfile", lambda: str(path))
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)
    return await packaging.build_export_zip(
        object(),
        context.project_id,
        batch_id=context.batch_id,
        targets=context.targets,
        include_attributes=True,
        video_frame_mode=context.format_options["video_frame_mode"],
        format_options=context.format_options,
        video_scope=context.video_scope,
        video_media=context,
        check_cancel=check_cancel,
    )


@pytest.mark.parametrize("original", [False, True])
async def test_all_video_formats_use_one_trusted_grid_receipt_and_separate_bases(
    monkeypatch, tmp_path, original
):
    frozen = context(original=original, mask=True)

    async def mask(reference):
        assert reference["sha256"] == "a" * 64
        return {"encoding": "coco_rle", "size": [3, 4], "counts": [0, 1, 11]}

    monkeypatch.setattr(packaging, "load_coco_rle", mask)
    monkeypatch.setattr("app.services.exporting.service.load_coco_rle", mask)
    path, _, _ = await archive(frozen, monkeypatch, tmp_path)
    try:
        with zipfile.ZipFile(path) as output:
            assert len(output.namelist()) == len(set(output.namelist()))
            manifest = json.loads(output.read("manifest.json"))
            entry = manifest["videos"][0]
            sequence = frozen.sequence_paths[frozen.tasks[0].id]
            assert entry["rel_path"] == frozen.paths[frozen.tasks[0].id]
            assert entry["grid_source_frames"] == [0, 3, 6, 9]
            assert (
                entry["metadata_receipt"]
                == frozen.metadata_receipts[frozen.tasks[0].id]
            )
            assert entry["sha256"] == SHA and entry["size_bytes"] == len(RAW)
            specifications = {row["dir"]: row for row in entry["frame_outputs"]}
            assert specifications[f"mot/{sequence}/img1"]["start_number"] == 1
            assert specifications[f"kitti/{sequence}/img1"]["start_number"] == 0
            assert specifications[f"youtube-vos/JPEGImages/{sequence}"][
                "source_frames"
            ] == [1, 7]
            assert (
                output.read(f"mot/{sequence}/gt/gt.txt")
                .decode()
                .splitlines()[0]
                .startswith("1,")
            )
            assert (
                output.read(f"kitti/labels/{sequence}.txt")
                .decode()
                .splitlines()[0]
                .startswith("0 ")
            )
            portable = json.loads(output.read("aap_json/annotations.json"))
            assert portable["tasks"][0]["video"]["frame_count"] == 10
            assert portable["tasks"][0]["video"]["metadata_receipt"]["sha256"] == SHA
            tracks = json.loads(output.read("video_json/annotations.json"))
            assert tracks["tasks"][0]["video_metadata"]["width"] == 4
            assert (
                max(frame["frame_index"] for frame in tracks["tracks"][0]["frames"])
                == 9
            )
            assert not any(
                "999999" in output.read(name).decode(errors="ignore")
                for name in output.namelist()
                if name.endswith(".json")
            )
    finally:
        Path(path).unlink()


@pytest.mark.parametrize("target,base", [("mot", 1), ("kitti", 0)])
async def test_single_layout_and_same_named_task_sequences_do_not_collide(
    monkeypatch, tmp_path, target, base
):
    frozen = context(targets=[target], original=True, same_name=True)
    path, _, _ = await archive(frozen, monkeypatch, tmp_path)
    try:
        with zipfile.ZipFile(path) as output:
            entries = json.loads(output.read("manifest.json"))["videos"]
            assert len({entry["sequence"] for entry in entries}) == 2
            assert len({entry["rel_path"] for entry in entries}) == 2
            for entry in entries:
                assert entry["frame_outputs"][0]["dir"] == entry["sequence"] + "/img1"
                assert entry["frame_outputs"][0]["start_number"] == base
    finally:
        Path(path).unlink()


@pytest.mark.parametrize(
    "maximum,targets",
    [
        ("MAX_SELECTED_FRAMES", ["mot"]),
        ("MAX_EVALUATIONS", ["video_json"]),
        ("MAX_RESOLVE_WORK", ["mot"]),
        ("MAX_ZIP_ENTRIES", ["yolo-frames-det"]),
        ("MAX_EXPANDED_BYTES", ["video_json"]),
    ],
)
async def test_budget_rejects_before_any_grid_or_zip_allocation(
    monkeypatch, tmp_path, maximum, targets
):
    frozen = context(targets=targets)
    monkeypatch.setattr(video_budget, maximum, 1)
    monkeypatch.setattr(packaging, "_new_zip_tempfile", forbidden)
    monkeypatch.setattr(packaging, "derive_sampled_frames", forbidden)
    with pytest.raises(ContentError, match="budget"):
        await packaging.build_export_zip(
            object(),
            frozen.project_id,
            batch_id=None,
            targets=frozen.targets,
            include_attributes=True,
            video_frame_mode="all_frames",
            video_media=frozen,
        )


def test_scope_and_out_of_source_keyframes_are_rejected_without_extending_length():
    frozen = context(targets=["video_json"])
    frozen.metadata_by_task[frozen.tasks[0].id]["frame_count"] = 9
    with pytest.raises(ContentError):
        video_budget.check(frozen)


@pytest.mark.parametrize(
    "maximum",
    [
        "MAX_RASTER_PIXELS",
        "MAX_PORTABLE_RUNS",
        "MAX_PORTABLE_JSON",
        "MAX_STRUCTURAL_ROWS",
        "MAX_VERTICES",
    ],
)
def test_raster_portable_and_structure_budgets_are_metadata_only(monkeypatch, maximum):
    frozen = context(targets=["davis", "aap_json"], mask=True)
    monkeypatch.setattr(video_budget, maximum, 1)
    monkeypatch.setattr(packaging, "load_coco_rle", forbidden)
    with pytest.raises(ContentError, match="budget"):
        video_budget.check(frozen)


def test_keyframes_json_does_not_consume_all_frames_or_raster_budget(monkeypatch):
    frozen = context(targets=["video_json"])
    frozen.format_options["video_frame_mode"] = "keyframes"
    monkeypatch.setattr(video_budget, "MAX_EVALUATIONS", 0)
    monkeypatch.setattr(video_budget, "MAX_RASTER_PIXELS", 0)
    assert video_budget.check(frozen)["evaluations"] == 0


def test_youtube_raster_budget_uses_exact_mask_frames():
    frozen = context(targets=["youtube-vos"], mask=True)
    totals = video_budget.check(frozen)
    assert totals["raster_pixels"] == 2 * 4 * 3 * 2
    assert totals["evaluations"] == 2


async def test_empty_video_project_keeps_valid_metadata_archive(monkeypatch, tmp_path):
    data = facts(original=True)
    data["tasks"], data["annotations"], data["media"] = [], [], []
    frozen = Context(data)
    frozen.format_options["video_frame_mode"] = "keyframes"
    path, _, _ = await archive(frozen, monkeypatch, tmp_path)
    try:
        with zipfile.ZipFile(path) as output:
            assert json.loads(output.read("manifest.json"))["videos"] == []
            assert json.loads(output.read("aap_json/annotations.json"))["tasks"] == []
    finally:
        Path(path).unlink()


async def test_cancelled_format_thread_exits_before_caller_can_remove_zip():
    started, stopped = threading.Event(), threading.Event()

    def writer(*, checkpoint):
        started.set()
        try:
            while True:
                checkpoint()
                time.sleep(0.001)
        finally:
            stopped.set()

    async def cancelled():
        return None

    task = asyncio.create_task(packaging._video_call(writer, check_cancel=cancelled))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


def fetcher(tmp_path):
    namespace = {
        "__name__": "frozen_fetch",
        "__file__": str(tmp_path / "fetch_videos.py"),
    }
    exec(
        compile(packaging._FETCH_VIDEOS_TEMPLATE, "fetch_videos.py", "exec"), namespace
    )
    return namespace


def download_manifest(*, relative="clip.mp4", expires=None):
    expires = expires or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {
        "expires_at": expires,
        "videos": [
            {
                "rel_path": relative,
                "presigned_url": "/saved?signature=private",
                "sha256": SHA,
                "size_bytes": len(RAW),
            }
        ],
    }


def test_fetch_videos_replaces_wrong_same_length_existing_bytes_atomically(tmp_path):
    script = fetcher(tmp_path)
    manifest = download_manifest()
    items = script["validated_items"](manifest, "https://platform.invalid")
    (tmp_path / "videos").mkdir()
    destination = tmp_path / "videos" / "clip.mp4"
    destination.write_bytes(b"wrong!!")

    class Response(io.BytesIO):
        status = 200
        headers = Message()

    Response.headers["Content-Length"] = str(len(RAW))
    script["download_response"] = lambda url: Response(RAW)
    script["fetch"](items[0])
    assert destination.read_bytes() == RAW
    script["download_response"] = forbidden
    script["fetch"](items[0])
    assert not list(tmp_path.rglob(".aap-download-*"))


@pytest.mark.parametrize("bad", [b"bad", b"wrong!!", RAW + b"extra"])
def test_fetch_videos_bad_download_preserves_existing_file_and_cleans_temporary(
    tmp_path, bad
):
    script = fetcher(tmp_path)
    item = script["validated_items"](download_manifest(), "https://platform.invalid")[0]
    destination = tmp_path / "videos" / "clip.mp4"
    destination.parent.mkdir()
    destination.write_bytes(b"retain!")

    class Response(io.BytesIO):
        status, headers = 200, Message()

    script["download_response"] = lambda url: Response(bad)
    with pytest.raises(script["Invalid"]):
        script["fetch"](item)
    assert destination.read_bytes() == b"retain!"
    assert not list(tmp_path.rglob(".aap-download-*"))


def test_fetch_video_profile_accepts_five_gib_but_not_arbitrary_archive_destinations(
    tmp_path,
):
    script = fetcher(tmp_path)
    value = download_manifest()
    value["videos"][0]["size_bytes"] = 5 * 1024**3
    assert (
        script["validated_items"](value, "https://platform.invalid")[0][2]
        == 5 * 1024**3
    )
    value["videos"][0]["archive_paths"] = ["manifest.json"]
    with pytest.raises(script["Invalid"]):
        script["validated_items"](value, "https://platform.invalid")
