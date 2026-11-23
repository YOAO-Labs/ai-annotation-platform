"""Frozen, preflight-owned differences; not-seen never means deleted."""

from app.schemas.dataset_source import DatasetSourceChangeList
from app.services import dataset_registration_data as data
from app.services.source_selection import selected_file


async def initialize(db, run, scan_config, location_kind):
    excluded = [
        row.path_digest
        for row in await data.unseen_paths(db, run.id)
        if not selected_file(scan_config, row.relative_path, location_kind)
    ]
    if excluded:
        await data.exclude_unseen(db, run.id, excluded)


async def page(db, run, kind, limit, offset):
    projection = await data.changes_page(db, run.id, kind, limit, offset)
    return DatasetSourceChangeList.model_validate(projection).model_dump()
