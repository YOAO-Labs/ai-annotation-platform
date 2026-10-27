"""Bind saved small-object owners to the established version-removal protocol."""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import storage_access
from app.services.export_media_removal_accounting import (
    Observation as Observation,
    Recorder as Recorder,
    finish as finish,
)


def recorder(db, receipt, *, producer, purpose, owner_id=None):
    ref = receipt.storage_ref
    return Recorder(
        async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
        producer,
        receipt.id if owner_id is None else owner_id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose=purpose,
    )
