import uuid

import pytest
from sqlalchemy import text

from tests.migration_helpers import bind, historical_schema, load


async def test_referenced_filesystem_location_refuses_downgrade(test_engine):
    async with historical_schema(test_engine, "0195") as (_schema, engine):
        async with engine.begin() as connection:
            location_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO storage_locations (id,name,kind,config) "
                    "VALUES (:id,'referenced-directory','filesystem','{}'::jsonb)"
                ),
                {"id": location_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO storage_bindings (purpose,location_id,bucket,generation) "
                    "VALUES ('datasets',:id,'unused',1)"
                ),
                {"id": location_id},
            )

            def refuse(sync):
                module = bind(load("0195_controlled_storage_locations.py"), sync)
                with pytest.raises(
                    RuntimeError, match="Retain referenced storage locations"
                ):
                    module.downgrade()
                assert (
                    sync.scalar(text("SELECT count(*) FROM storage_location_probes"))
                    == 0
                )
                assert sync.scalar(text("SELECT count(*) FROM storage_bindings")) == 1

            await connection.run_sync(refuse)
