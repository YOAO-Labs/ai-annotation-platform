"""Owned transport seam for existing unversioned, non-observing fake stores."""

from types import SimpleNamespace

from botocore.config import Config

from app.services import storage


class _Owned:
    """Delegate every provider call to the fixture's in-memory client."""

    def __init__(self, client, client_config):
        self._client = client
        self.meta = SimpleNamespace(
            endpoint_url=client.meta.endpoint_url, config=client_config
        )

    def __getattr__(self, name):
        # Resolve at invocation so blocking/error injection still applies.
        return getattr(self._client, name)

    def close(self):
        return None


def patch_owned_client(monkeypatch, client):
    if not isinstance(getattr(client.meta, "config", None), Config):
        client.meta.config = Config(retries={"max_attempts": 3})

    def create(*, client_config):
        assert client_config is client.meta.config
        return SimpleNamespace(client=_Owned(client, client_config))

    monkeypatch.setattr(storage, "StorageService", create)
