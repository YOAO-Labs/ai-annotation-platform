"""Borrow the caller Session for Labeling-owned class-usage row sets."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from yoao_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_policy,
)
from yoao_labeling.class_query_postgres import CLASS_QUERY_SCHEMA

_POLICY = text(f"SELECT {CLASS_QUERY_SCHEMA}.class_query_policy()")
_CLASS_COUNTS = text(
    f"SELECT class_name,annotation_count FROM {CLASS_QUERY_SCHEMA}.class_counts(CAST(:project_id AS uuid))"
)
_ATTRIBUTE_ROWS = text(
    f"SELECT attributes_json FROM {CLASS_QUERY_SCHEMA}.attribute_rows(CAST(:project_id AS uuid))"
)


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
            policy = (await self._execute(_POLICY)).scalar()
            decode_labeling_class_query_policy(policy)
            self._policy_checked = True

    async def class_rows(self, project_id):
        await self._prepare()
        rows = await self._execute(_CLASS_COUNTS, {"project_id": project_id})
        return [tuple(row) for row in rows.all()]

    async def attribute_rows(self, project_id):
        await self._prepare()
        rows = await self._execute(_ATTRIBUTE_ROWS, {"project_id": project_id})
        return list(rows.scalars().all())

    async def _execute(self, statement, parameters=None):
        try:
            with self.db.no_autoflush:
                return await self.db.execute(statement, parameters)
        except SQLAlchemyError:
            raise LabelingClassQueryUnavailable() from None
