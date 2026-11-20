from __future__ import annotations

import ast

from tests.jobs_quality_module_helpers import (
    _API_DISPATCH_PROBE,
    _QUALITY_TASKS,
)


def test_quality_dispatch_probe_compiles_and_covers_six_variants():
    ast.parse(_API_DISPATCH_PROBE)
    assert [item["operation"] for item in _QUALITY_TASKS] == [
        "run",
        "run",
        "run",
        "repair",
        "repair",
        "import",
    ]
    assert len({item["task"] for item in _QUALITY_TASKS}) == 6
