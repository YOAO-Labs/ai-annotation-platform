"""Bind the local scale-seed script to fixed Labeling capabilities."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class LabelingScaleSeedOperations:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def purge_labeling_rows(
        self, marker: str, project_ids: list[uuid.UUID]
    ) -> None:
        await self._db.execute(
            text(
                "SELECT aap_labeling_v1.purge_scale_seed_labeling_rows("
                "CAST(:marker AS text),CAST(:project_ids AS uuid[]))"
            ),
            {"marker": marker, "project_ids": project_ids},
        )

    async def create_batches(
        self,
        marker: str,
        project_id: uuid.UUID,
        dataset_id: uuid.UUID,
        batch_count: int,
        annotator_id: uuid.UUID,
    ) -> int:
        return await self._db.scalar(
            text(
                "SELECT aap_labeling_v1.create_scale_seed_batches("
                "CAST(:marker AS text),CAST(:project_id AS uuid),"
                "CAST(:dataset_id AS uuid),"
                "CAST(:batch_count AS integer),CAST(:annotator_id AS uuid))"
            ),
            {
                "marker": marker,
                "project_id": project_id,
                "dataset_id": dataset_id,
                "batch_count": batch_count,
                "annotator_id": annotator_id,
            },
        )

    async def create_tasks(
        self, marker: str, project_id: uuid.UUID, dataset_id: uuid.UUID
    ) -> int:
        return await self._db.scalar(
            text(
                "SELECT aap_labeling_v1.create_scale_seed_tasks("
                "CAST(:marker AS text),CAST(:project_id AS uuid),"
                "CAST(:dataset_id AS uuid))"
            ),
            {"marker": marker, "project_id": project_id, "dataset_id": dataset_id},
        )

    async def create_annotations(
        self,
        marker: str,
        project_id: uuid.UUID,
        annotator_id: uuid.UUID,
        annotation_count: int,
    ) -> int:
        return await self._db.scalar(
            text(
                "SELECT aap_labeling_v1.create_scale_seed_annotations("
                "CAST(:marker AS text),CAST(:project_id AS uuid),"
                "CAST(:annotator_id AS uuid),"
                "CAST(:annotation_count AS integer))"
            ),
            {
                "marker": marker,
                "project_id": project_id,
                "annotator_id": annotator_id,
                "annotation_count": annotation_count,
            },
        )

    async def read_first_batch_id(self, project_id: uuid.UUID) -> uuid.UUID | None:
        return await self._db.scalar(
            text(
                "SELECT id FROM aap_labeling_v1.batch_rows "
                "WHERE project_id=CAST(:project_id AS uuid) "
                "ORDER BY display_id LIMIT 1"
            ),
            {"project_id": project_id},
        )
