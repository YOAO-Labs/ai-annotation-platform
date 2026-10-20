"""v0.14.11 · capability_registry SSOT 单测.

校验:
- 受控词表与 aap_inference.capabilities re-export 一致 (防回归);
- task / infra / modality / geometry 元数据完整 (label / summary 非空);
- task.default_geometry 与 _TASK_DEFAULT_GEOMETRY 一致;
- suggested_backends.repo_url 是合法 https URL;
- suggested_backends.research_link (非 null) 在仓库内存在。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from aap_inference import capability_registry as reg
from aap_inference import capabilities as caps


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_backend_vocab():
    """从文件加载 backend 侧镜像 vocab.py (不引入 protocol_v2 → apps/api 反向依赖).

    vocab.py 纯常量、无第三方依赖, 故可独立 exec, 不必把整个包加进 pythonpath。
    """
    path = REPO_ROOT / "apps/_shared/protocol_v2/src/aap_protocol_v2/vocab.py"
    spec = importlib.util.spec_from_file_location("_aap_protocol_v2_vocab", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_modality_aligned_with_derive_modalities():
    """modality 受控表与 derive_modalities 实际输出对齐 (image/video/lidar)。"""
    assert reg.MODALITY_VALUES == ("image", "video", "lidar")


def test_geometry_count_and_ids():
    assert len(reg.GEOMETRIES) == 9
    assert {"bbox", "mask", "none"}.issubset(reg.GEOMETRY_VALUES)


def test_all_tasks_have_non_empty_metadata():
    for t in reg.TASKS:
        assert t.label, f"task {t.id} 缺 label"
        assert t.summary, f"task {t.id} 缺 summary"
        assert t.protocol_notes, f"task {t.id} 缺 protocol_notes"


def test_task_default_geometry_matches_re_export():
    assert reg.TASK_DEFAULT_GEOMETRY == caps._TASK_DEFAULT_GEOMETRY  # noqa: SLF001
    for t in reg.TASKS:
        assert list(t.default_geometry) == reg.TASK_DEFAULT_GEOMETRY[t.id]


def test_backend_vocab_mirror_matches_registry():
    """vocab.py 自称是 capability_registry 的最小镜像; 锁死防漂移 (v0.14.17).

    backend 进程不反向依赖 apps/api, 无法在 protocol_v2 包内断言; 由 apps/api 侧
    (唯一同时知道两边的地方) 用文件加载核对 5 张受控词表 + TASK_DEFAULT_GEOMETRY。
    历史上 ocr / tracker 的默认几何在两边漂移过 (PR #35 审查发现)。
    """
    vocab = _load_backend_vocab()
    assert vocab.TASK_VALUES == reg.TASK_VALUES
    assert vocab.INFRA_VALUES == reg.INFRA_VALUES
    assert vocab.GEOMETRY_VALUES == reg.GEOMETRY_VALUES
    assert vocab.PROMPT_VALUES == reg.PROMPT_VALUES
    assert vocab.INPUT_VALUES == reg.INPUT_VALUES
    # registry 侧值是 list、vocab 侧是 tuple, 归一化为 tuple 后逐 task 比较。
    reg_geom = {k: tuple(v) for k, v in reg.TASK_DEFAULT_GEOMETRY.items()}
    vocab_geom = {k: tuple(v) for k, v in vocab.TASK_DEFAULT_GEOMETRY.items()}
    assert vocab_geom == reg_geom


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
