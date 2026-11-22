"""Let task image derivative admission share the original Item read fence."""

import sqlalchemy as sa
from alembic import op

revision = "bo0482"
down_revision = "bo0481"
branch_labels = depends_on = None


_TASK_ITEM_LOCK = """WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid
                FOR {} NOWAIT;"""


def _replace_task_item_lock(old: str, new: str) -> None:
    db = op.get_bind()
    definition = db.scalar(
        sa.text(
            "SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS pg_catalog.regprocedure))"
        ),
        {
            "signature": "yoao_assets_v1._image_owner_source_share(uuid,uuid,uuid,boolean)"
        },
    )
    before, after = _TASK_ITEM_LOCK.format(old), _TASK_ITEM_LOCK.format(new)
    if definition.count(before) != 1 or after in definition:
        raise RuntimeError("Unexpected image Task Item admission fence")
    db.exec_driver_sql(definition.replace(before, after, 1))


def upgrade() -> None:
    _replace_task_item_lock("UPDATE", "SHARE")


def downgrade() -> None:
    _replace_task_item_lock("SHARE", "UPDATE")
