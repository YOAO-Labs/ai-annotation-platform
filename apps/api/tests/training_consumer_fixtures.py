"""Keep existing fake-provider lifecycle tests at the transport boundary."""

import asyncio

from app.services import training_storage_io
from app.services.storage import storage_service


def mock_legacy_transport(monkeypatch):
    # Current-target metadata/permit behavior has dedicated storage tests. These
    # fixtures run inside rolled-back business transactions with fake providers.
    async def run(db, owner, purpose, callback, *args, accounting=None, **kwargs):
        return await asyncio.to_thread(callback, storage_service, *args, **kwargs)

    async def prepare(db, owner, **kwargs):
        return owner

    def physical(target, callback, *args, accounting=None, **kwargs):
        return callback(storage_service, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", run)
    monkeypatch.setattr(training_storage_io, "prepare", prepare)
    monkeypatch.setattr(training_storage_io, "physical", physical)
