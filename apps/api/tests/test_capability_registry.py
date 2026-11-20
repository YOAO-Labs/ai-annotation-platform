"""Public capability re-exports, registry order and repository metadata."""

from __future__ import annotations

from pathlib import Path

from yoao_inference import capability_registry as reg
from yoao_inference import capabilities as caps


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_re_export_aligned():
    assert caps.INFRA_VALUES == reg.INFRA_VALUES
    assert caps.TASK_VALUES == reg.TASK_VALUES
    assert caps.GEOMETRY_VALUES == reg.GEOMETRY_VALUES
    assert caps.INPUT_VALUES == reg.INPUT_VALUES


def test_protocol_v2_task_count_and_ids():
    """The protocol lists the nine canonical tasks in contract order."""
    assert len(reg.TASKS) == 9
    assert reg.TASK_VALUES == (
        "detect",
        "segment",
        "classify",
        "pose",
        "obb",
        "ocr",
        "doc_layout",
        "tracker",
        "interactive_seg",
    )


def test_protocol_v2_infra_count_and_ids():
    assert len(reg.INFRAS) == 6
    assert reg.INFRA_VALUES == (
        "pytorch",
        "onnx",
        "paddle",
        "tensorrt",
        "openvino",
        "other",
    )


def test_geometry_count_and_ids():
    assert len(reg.GEOMETRIES) == 9
    assert {"bbox", "mask", "none"}.issubset(reg.GEOMETRY_VALUES)


def test_all_tasks_have_non_empty_metadata():
    for t in reg.TASKS:
        assert t.label, f"task {t.id} 缺 label"
        assert t.summary, f"task {t.id} 缺 summary"
        assert t.protocol_notes, f"task {t.id} 缺 protocol_notes"


def test_suggested_backend_url_is_https():
    for t in reg.TASKS:
        for s in t.suggested_backends:
            assert s.repo_url.startswith("https://"), (
                f"task={t.id} backend={s.name} repo_url 非 https: {s.repo_url}"
            )


def test_suggested_backend_research_link_exists_in_repo():
    """research_link 非 null 时必须指向仓库内真实文件 (防 docs 路径漂移)。"""
    missing: list[str] = []
    for t in reg.TASKS:
        for s in t.suggested_backends:
            if s.research_link is None:
                continue
            target = REPO_ROOT / s.research_link
            if not target.exists():
                missing.append(f"task={t.id} backend={s.name} → {s.research_link}")
    assert not missing, "research_link 失效:\n" + "\n".join(missing)
