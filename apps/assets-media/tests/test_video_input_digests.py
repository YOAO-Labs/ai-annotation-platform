import json
from pathlib import Path
import subprocess
from uuid import UUID

import pytest

from aap_assets_video_inputs import digests as d, execution as c
from video_input_fixtures import manifest, member, record


def vector():
    ready = member("ready")
    ready = ready.model_copy(
        update={
            "source": ready.source.model_copy(
                update={"file_path": "数据/原始;视频.mp4"}
            )
        }
    )
    missing_task = ready.task.model_copy(
        update={
            "task_id": UUID("00000000-0000-4000-8000-000000000044"),
            "dataset_item_id": None,
            "file_path": "-",
        }
    )
    failed = c.VideoManifestMember(
        ordinal=2,
        task=missing_task,
        source=None,
        state="failed",
        input=None,
        error="source_missing",
    )
    header = manifest().model_copy(
        update={
            "selection": manifest().selection.model_copy(update={"task_ids": None}),
            "candidate_count": 2,
            "selected_count": 2,
            "prepared_count": 2,
        }
    )
    captures = tuple(
        c.CapturedVideoMember(
            ordinal=m.ordinal,
            task=m.task,
            source=m.source,
            error=m.error if m.state == "failed" else None,
        )
        for m in (ready, failed)
    )
    scope = c.VideoManifestScope(
        job_id=header.job_id, actor_id=header.actor_id, project_id=header.project_id
    )
    selection = d.selection_digest(scope, header.selection, 2, 2, iter(captures))
    header = header.model_copy(update={"selection_digest": selection})
    sealed = d.sealed_digest(header, 1, 1, iter((ready, failed)))
    return {
        "scope": scope.model_dump(mode="json"),
        "selection": header.selection.model_dump(mode="json"),
        "candidate_count": 2,
        "selected_count": 2,
        "captures": [capture.model_dump(mode="json") for capture in captures],
        "header": header.model_dump(mode="json"),
        "ready_count": 1,
        "failed_count": 1,
        "members": [m.model_dump(mode="json") for m in (ready, failed)],
        "selection_digest": selection,
        "sealed_digest": sealed,
    }


def test_independent_node_reproduces_utf8_null_failed_and_ready_manifest_hashes():
    case = vector()
    assert case == json.loads(
        (
            Path(__file__).with_name("fixtures") / "video-input-manifest-vector.json"
        ).read_text()
    )
    result = subprocess.run(
        [
            "node",
            str(
                Path(__file__).with_name("fixtures") / "video-input-manifests-peer.mjs"
            ),
        ],
        input=json.dumps(case, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        key: case[key] for key in ("selection_digest", "sealed_digest")
    }


@pytest.mark.parametrize(
    "fault", ["missing", "repeat", "order", "source", "error", "path", "identity"]
)
def test_selection_digest_covers_complete_immutable_capture(fault):
    raw = vector()
    captures = [
        c.CapturedVideoMember.model_validate(value) for value in raw["captures"]
    ]
    if fault == "missing":
        captures.pop()
    elif fault == "repeat":
        captures[1] = captures[0]
    elif fault == "order":
        captures.reverse()
    elif fault == "source":
        captures[0] = captures[0].model_copy(
            update={
                "source": captures[0].source.model_copy(
                    update={"timetable_revision": 6}
                )
            }
        )
    elif fault == "error":
        captures[1] = captures[1].model_copy(update={"error": "source_changed"})
    elif fault == "path":
        captures[0] = captures[0].model_copy(
            update={
                "source": captures[0].source.model_copy(
                    update={"file_path": "不同/原始.mp4"}
                )
            }
        )
    else:
        captures[0] = captures[0].model_copy(
            update={
                "source": captures[0].source.model_copy(
                    update={"object_identity": record().source.object_identity}
                )
            }
        )
    try:
        changed = d.selection_digest(
            c.VideoManifestScope.model_validate(raw["scope"]),
            c.VideoSelection.model_validate(raw["selection"]),
            2,
            2,
            iter(captures),
        )
    except ValueError:
        return
    assert changed != raw["selection_digest"]


def test_historical_byte_expiry_keeps_the_same_sealed_outcome_digest():
    raw = vector()
    header = c.VideoManifestHeader.model_validate(raw["header"])
    ready, failed = [
        c.VideoManifestMember.model_validate(value) for value in raw["members"]
    ]
    header = header.model_copy(
        update={
            "status": "sealed",
            "ready_count": 1,
            "failed_count": 1,
            "sealed_digest": raw["sealed_digest"],
            "sealed_at": header.created_at,
        }
    )
    ready = ready.model_copy(
        update={"input": ready.input.model_copy(update={"status": "expired"})}
    )
    assert d.sealed_digest(header, 1, 1, iter((ready, failed))) == raw["sealed_digest"]


def test_hash_field_encoding_distinguishes_null_empty_and_delimiter_strings():
    assert len({d._fields([value]) for value in (None, "", "-", "N;", "S0:;")}) == 5
    assert d._fields(["视频"]) == b"S6:" + "视频".encode() + b";"
