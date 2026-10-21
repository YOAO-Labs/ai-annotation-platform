"""Capture the real pre-extraction Project date and row-column boundary once."""

from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text

from app.db.models.project import Project
from tests.test_asset_storage_cleanup_runtime import _verify_postgres

_REFERENCE = "50fc1df9ac9f99fdbbed7ac0eda7205d7287156d"
_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_PROJECT_QUERY_CODEC_CAPTURE") != "1",
        reason="requires explicit one-time pre-port capture on owned resources",
    ),
]


async def test_capture_legacy_project_date_transport(test_engine):
    _verify_postgres(test_engine.url)
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    output = _ROOT / ".worktree/workspace-project-query-legacy-scalar-codecs.json"
    assert not output.exists(), "Keep the pre-port codec capture immutable"
    relative = "apps/api/app/db/models/project.py"
    original = subprocess.check_output(
        ["rtk", "proxy", "git", "show", f"{_REFERENCE}:{relative}"], cwd=_ROOT
    )
    assert original == (_ROOT / relative).read_bytes(), (
        "Project schema changed before capture"
    )
    observations = []
    async with test_engine.connect() as db:
        try:
            for style in ("ISO, MDY", "SQL, DMY"):
                await db.execute(
                    text("SELECT pg_catalog.set_config('DateStyle',:style,true)"),
                    {"style": style},
                )
                row = (
                    (
                        await db.execute(
                            text(
                                "SELECT DATE '2020-01-02' AS finite, "
                                "DATE '-infinity' AS minimum, DATE 'infinity' AS maximum, "
                                "pg_catalog.jsonb_build_object("
                                "'finite',DATE '2020-01-02','minimum',DATE '-infinity',"
                                "'maximum',DATE 'infinity') AS native_json"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["finite"] == date(2020, 1, 2)
                assert row["minimum"] == date.min and row["maximum"] == date.max
                raw = row["native_json"]
                if isinstance(raw, str):
                    raw = json.loads(raw)
                observations.append(
                    {
                        "date_style": await db.scalar(text("SHOW DateStyle")),
                        "binary": {
                            key: {
                                "type": type(row[key]).__name__,
                                "iso": row[key].isoformat(),
                            }
                            for key in ("finite", "minimum", "maximum")
                        },
                        "native_json": raw,
                    }
                )
        finally:
            await db.rollback()
    document = {
        "runtime_reference_commit": _REFERENCE,
        "project_model_sha256": hashlib.sha256(original).hexdigest(),
        "project_physical_columns": [
            column.name for column in Project.__table__.columns
        ],
        "observations": observations,
    }
    with output.open("x") as stream:
        json.dump(document, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")
