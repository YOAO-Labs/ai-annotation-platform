"""Cross-path predecessor constraints preserve business identity and source ownership."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset_source import DatasetSourceRevision
from app.services.dataset_references import register_entry
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_source_relocations import moved_candidate


async def test_cross_path_predecessor_rejects_changed_identity_and_source(
    db_session, content_world
):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, old, target, payload = await moved_candidate(db, world)
    previous = await db.get(DatasetSourceRevision, old["before_revision_id"])
    facts = dict(
        source_id=previous.source_id,
        path_digest=target["path_digest"],
        run_id=world[6].id,
        revision_number=1,
        item_id_at_accept=previous.item_id_at_accept,
        asset_id_at_accept=previous.asset_id_at_accept,
        dataset_item_id=previous.dataset_item_id,
        asset_id=previous.asset_id,
        content_sha256=previous.content_sha256,
        ownership_mode=previous.ownership_mode,
        owns_item=previous.owns_item,
        previous_revision_id=previous.id,
    )
    for field, value, error in [
        ("source_id", uuid.uuid4(), "predecessor source differs"),
        ("item_id_at_accept", uuid.uuid4(), "preserve business identity"),
        ("asset_id_at_accept", uuid.uuid4(), "preserve business identity"),
        ("content_sha256", "f" * 64, "preserve business identity"),
        ("ownership_mode", "managed_copy", "preserve business identity"),
        ("owns_item", False, "preserve business identity"),
    ]:
        with pytest.raises(IntegrityError, match=error):
            async with db.begin_nested():
                await db.execute(
                    DatasetSourceRevision.__table__.insert().values(
                        id=uuid.uuid4(), **{**facts, field: value}
                    )
                )
