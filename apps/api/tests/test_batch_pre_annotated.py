"""Batch 状态机 pre_annotated 状态合法性。

pre_annotated 是 AI 文本批量预标完成、待人工接管的中间态。
- ACTIVE → PRE_ANNOTATED：仅 batch_predict task 内部驱动
- PRE_ANNOTATED → ANNOTATING：与 ACTIVE → ANNOTATING 同语义，scheduler 自动驱动
- PRE_ANNOTATED → ACTIVE：owner 兜底逆向（丢弃 predictions 重置）
- PRE_ANNOTATED → ARCHIVED：owner 任意

非合法迁移示例：PRE_ANNOTATED → REVIEWING / APPROVED 必须先经 ANNOTATING。
"""

from __future__ import annotations

from app.db.enums import BatchStatus
from app.services.batch import REVERSE_TRANSITIONS, VALID_TRANSITIONS


def test_pre_annotated_in_valid_transitions():
    assert BatchStatus.PRE_ANNOTATED in VALID_TRANSITIONS


def test_active_can_transition_to_pre_annotated():
    assert BatchStatus.PRE_ANNOTATED in VALID_TRANSITIONS[BatchStatus.ACTIVE]


def test_pre_annotated_can_transition_to_annotating():
    assert BatchStatus.ANNOTATING in VALID_TRANSITIONS[BatchStatus.PRE_ANNOTATED]


def test_pre_annotated_can_transition_back_to_active():
    """owner 兜底逆向：丢弃 AI 预标重置。"""
    assert BatchStatus.ACTIVE in VALID_TRANSITIONS[BatchStatus.PRE_ANNOTATED]
    assert (BatchStatus.PRE_ANNOTATED, BatchStatus.ACTIVE) in REVERSE_TRANSITIONS


def test_pre_annotated_can_archive():
    assert BatchStatus.ARCHIVED in VALID_TRANSITIONS[BatchStatus.PRE_ANNOTATED]


def test_pre_annotated_cannot_skip_to_reviewing():
    """必须先经 ANNOTATING（人工接管），跳级到 REVIEWING 非法。"""
    assert BatchStatus.REVIEWING not in VALID_TRANSITIONS[BatchStatus.PRE_ANNOTATED]


def test_pre_annotated_cannot_skip_to_approved():
    assert BatchStatus.APPROVED not in VALID_TRANSITIONS[BatchStatus.PRE_ANNOTATED]


def test_draft_cannot_jump_to_pre_annotated():
    """draft 必须先 active 再批量预标。"""
    assert BatchStatus.PRE_ANNOTATED not in VALID_TRANSITIONS[BatchStatus.DRAFT]
