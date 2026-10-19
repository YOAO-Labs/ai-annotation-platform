"""Shared search and UTC calendar-date bounds for project training lists."""

import re
from datetime import date, datetime, time, timezone
from typing import Annotated

from fastapi import HTTPException
from pydantic import BeforeValidator
from sqlalchemy import case, func
from aap_task_contract import TASK_ALIASES


def _iso_date(value):
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("创建日期须为 YYYY-MM-DD 格式")
    return value


ISODate = Annotated[date, BeforeValidator(_iso_date)]


def literal_search_pattern(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def created_date_bounds(
    created_from: date | None, created_to: date | None
) -> tuple[datetime | None, datetime | None]:
    if (
        created_from is not None
        and created_to is not None
        and created_from > created_to
    ):
        raise HTTPException(
            status_code=422, detail="创建日期的开始日期不能晚于结束日期"
        )
    return (
        datetime.combine(created_from, time.min, timezone.utc)
        if created_from
        else None,
        datetime.combine(created_to, time.max, timezone.utc) if created_to else None,
    )


def run_task_expression(config):
    task = func.coalesce(
        config["task"].as_string(),
        config["configuration"]["identity"]["task"].as_string(),
        "detect",
    )
    return case(TASK_ALIASES, value=task, else_=task)
