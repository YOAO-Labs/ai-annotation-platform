"""Private query table enforces database ownership keys and cascade foreign keys."""

from sqlalchemy import inspect


async def test_saved_views_schema_constraints(db_session):
    def verify(sync):
        schema = inspect(sync.connection())
        assert any(
            row["column_names"] == ["dataset_id", "owner_id", "name"]
            for row in schema.get_unique_constraints("dataset_saved_views")
        )
        assert {
            fk["referred_table"]
            for fk in schema.get_foreign_keys("dataset_saved_views")
        } == {"datasets", "users"}
        assert all(
            fk["options"]["ondelete"] == "CASCADE"
            for fk in schema.get_foreign_keys("dataset_saved_views")
        )

    await db_session.run_sync(verify)
