"""Explicit fresh-install process acceptance; not part of source-only selection."""

import os
from pathlib import Path

import pytest

from tests.test_inference_scope_retirement_live import (
    test_retire_one_live_scope_preserves_another_and_fences_delayed_tokens as _exercise,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data environment",
    ),
]


async def test_noneditable_owner_restarts_without_repeating_retirement(
    test_engine, tmp_path
):
    python = Path(os.environ["AAP_INFERENCE_SCOPE_RETIREMENT_INSTALLED_PYTHON"])
    assert python.is_absolute() and python.is_file()
    await _exercise(test_engine, tmp_path, "background", True, installed_python=python)
