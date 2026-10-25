"""Owned transport seam for existing unversioned, non-observing fake stores."""

from types import SimpleNamespace

from botocore.config import Config

from app.services import storage


def patch_owned_client(monkeypatch, client):
    client.meta.config = Config(retries={"max_attempts": 3})

    def create(*, client_config):
        assert client_config is client.meta.config
        owned = SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=client.meta.endpoint_url, config=client_config
            ),
            close=lambda: None,
        )
        # Resolve methods at invocation so blocking/error injection still applies.
        for name in ("put_object", "get_object", "head_object", "delete_object"):
            setattr(
                owned,
                name,
                lambda _name=name, **params: getattr(client, _name)(**params),
            )
        return SimpleNamespace(client=owned)

    monkeypatch.setattr(storage, "StorageService", create)
