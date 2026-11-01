"""Managed original test client and staging setup."""

from types import SimpleNamespace

from app.services import managed_original_creation as service
from tests.managed_original_provider_support import MemoryS3


class Client(MemoryS3):
    def __init__(self):
        super().__init__()
        self.closed = 0
        self.close_failure = False

    def close(self):
        self.closed += 1
        if self.close_failure:
            raise OSError("private close detail")


def stage_client(monkeypatch, value):
    scheme = "https" if service.settings.minio_use_ssl else "http"
    value.meta = SimpleNamespace(
        endpoint_url=f"{scheme}://{service.settings.minio_endpoint}"
    )
    monkeypatch.setattr(service.storage_service, "client", value)
