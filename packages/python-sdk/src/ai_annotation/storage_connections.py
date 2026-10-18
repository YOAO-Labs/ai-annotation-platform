"""Administrator storage connection CRUD and allowlist routes."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _idempotency_headers
from ai_annotation.storage_connection_models import (
    ConnectorAllowlist,
    ConnectorAllowlistUpdate,
    DeploymentSFTPStoragePreset,
    StorageConnection,
    StorageConnectionCreate,
    StorageConnectionTestResult,
    StorageConnectionUpdate,
)

IdLike = str | UUID


class StorageConnections:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self) -> list[StorageConnection]:
        resp = self._http.request("GET", "/storage-connections")
        return [StorageConnection.model_validate(item) for item in resp.json()]

    def create(
        self,
        body: StorageConnectionCreate,
        *,
        idempotency_key: IdLike | None = None,
    ) -> StorageConnection:
        resp = self._http.request(
            "POST",
            "/storage-connections",
            json=body.model_dump(mode="json", exclude_unset=True),
            headers=_idempotency_headers(idempotency_key),
        )
        return StorageConnection.model_validate(resp.json())

    def get(self, conn_id: IdLike) -> StorageConnection:
        resp = self._http.request("GET", f"/storage-connections/{conn_id}")
        return StorageConnection.model_validate(resp.json())

    def update(
        self,
        conn_id: IdLike,
        body: StorageConnectionUpdate,
        *,
        idempotency_key: IdLike | None = None,
    ) -> StorageConnection:
        resp = self._http.request(
            "PATCH",
            f"/storage-connections/{conn_id}",
            json=body.model_dump(mode="json", exclude_unset=True),
            headers=_idempotency_headers(idempotency_key),
        )
        return StorageConnection.model_validate(resp.json())

    def delete(self, conn_id: IdLike, *, idempotency_key: IdLike | None = None) -> None:
        self._http.request(
            "DELETE",
            f"/storage-connections/{conn_id}",
            headers=_idempotency_headers(idempotency_key),
        )

    def test(
        self, conn_id: IdLike, *, idempotency_key: IdLike | None = None
    ) -> StorageConnectionTestResult:
        resp = self._http.request(
            "POST",
            f"/storage-connections/{conn_id}/test",
            headers=_idempotency_headers(idempotency_key),
        )
        return StorageConnectionTestResult.model_validate(resp.json())

    def get_allowlist(self) -> ConnectorAllowlist:
        resp = self._http.request("GET", "/storage-connections/allowlist")
        return ConnectorAllowlist.model_validate(resp.json())

    def update_allowlist(self, body: ConnectorAllowlistUpdate) -> ConnectorAllowlist:
        resp = self._http.request(
            "PUT",
            "/storage-connections/allowlist",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return ConnectorAllowlist.model_validate(resp.json())

    def reset_allowlist(self) -> ConnectorAllowlist:
        resp = self._http.request("DELETE", "/storage-connections/allowlist")
        return ConnectorAllowlist.model_validate(resp.json())

    def deployment_sftp_preset(self) -> DeploymentSFTPStoragePreset:
        resp = self._http.request("GET", "/storage-connections/deployment-sftp-preset")
        return DeploymentSFTPStoragePreset.model_validate(resp.json())
