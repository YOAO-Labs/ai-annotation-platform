"""开集文本路径辅助函数单测 (不需 ultralytics / GPU)."""

from __future__ import annotations

from predictor import _parse_open_classes


def test_parse_comma_separated() -> None:
    assert _parse_open_classes("person, bus, car") == ["person", "bus", "car"]


def test_parse_preserves_order() -> None:
    """顺序即 cls index 映射, 不可排序."""
    assert _parse_open_classes("dog, cat, bird") == ["dog", "cat", "bird"]


def test_parse_newline_and_semicolon() -> None:
    assert _parse_open_classes("a\nb; c") == ["a", "b", "c"]


def test_parse_strips_whitespace_and_blanks() -> None:
    assert _parse_open_classes("  person ,, , bus  ") == ["person", "bus"]


def test_parse_multiword_class_kept() -> None:
    assert _parse_open_classes("traffic light, stop sign") == [
        "traffic light",
        "stop sign",
    ]


def test_parse_empty_returns_empty() -> None:
    assert _parse_open_classes("") == []
    assert _parse_open_classes(None) == []
    assert _parse_open_classes("   ") == []
