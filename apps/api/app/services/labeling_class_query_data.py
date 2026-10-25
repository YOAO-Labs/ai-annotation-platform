"""Borrow the caller Session for Labeling-owned class-usage row sets."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_policy,
)
from aap_labeling.class_query_postgres import CLASS_QUERY_SCHEMA


class LabelingClassQueryData:
    def __init__(self, db):
        self.db = db
        self._policy_checked = False

    @asynccontextmanager
    async def transaction(self):
        self._policy_checked = False
        yield self

    async def _prepare(self):
        if self.db.autoflush:
            await self.db.flush()
        if not self._policy_checked:
            try:
                policy = await self.db.scalar(
                    text(f"SELECT {CLASS_QUERY_SCHEMA}.class_query_policy()")
                )
            except SQLAlchemyError:
                raise LabelingClassQueryUnavailable() from None
            decode_labeling_class_query_policy(policy)
            self._policy_checked = True

    async def class_rows(self, project_id):
        await self._prepare()
        with self.db.no_autoflush:
            rows = await self.db.execute(
                text(
                    f"SELECT class_name,annotation_count FROM {CLASS_QUERY_SCHEMA}.class_counts(CAST(:project_id AS uuid))"
                ),
                {"project_id": project_id},
            )
        return [tuple(row) for row in rows.all()]

    async def attribute_rows(self, project_id):
        await self._prepare()
        with self.db.no_autoflush:
            rows = await self.db.execute(
                text(
                    f"SELECT attributes_json FROM {CLASS_QUERY_SCHEMA}.attribute_rows(CAST(:project_id AS uuid))"
                ),
                {"project_id": project_id},
            )
        return list(rows.scalars().all())
