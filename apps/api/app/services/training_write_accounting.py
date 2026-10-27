"""Training-owned overwrite evidence uses the existing managed upload protocol."""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import media_cache_accounting, storage_access
from app.services.conditional_write_accounting import finish as finish
from app.services.export_zip_accounting import Observation as Observation


def recorder(db, owner, owner_id, *, producer, purpose):
    ref = owner.ref
    return media_cache_accounting.Recorder(
        async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
        producer,
        owner_id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose=purpose,
    )
