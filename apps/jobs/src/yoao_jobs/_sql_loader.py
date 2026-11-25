"""Resolve Jobs SQL artifacts from packaged resources or apps/api/alembic/sql."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def read_jobs_sql(name: str) -> str:
    packaged = files("yoao_jobs").joinpath("sql", name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    canonical = Path(__file__).resolve().parents[3] / "api" / "alembic" / "sql" / name
    if canonical.is_file():
        return canonical.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Jobs SQL artifact not found: {name}")
