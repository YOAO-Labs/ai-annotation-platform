"""Strict, deployment-scoped managed checkpoint installation identity."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .provider import validate_receipt


def install_request_model(allowed_sources):
    # Exact origins, including port and scheme. No wildcard or implicit private hosts.
    origins = frozenset(allowed_sources)
    if not origins:
        raise ValueError("At least one managed artifact origin is required")
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
            raise ValueError("Invalid approved artifact origin")

    class InstallRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        model_version_id: UUID
        sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        classes_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
        classes: list[str] = Field(min_length=1, max_length=1000)
        framework: Literal["libreyolo"]
        framework_version: Literal["1.6.0"]
        task: Literal["detection"]
        size_bytes: int = Field(gt=0, le=2 * 1024**3)
        download_url: str = Field(max_length=10000)
        model_family: str
        model_size: str
        artifact_format: str
        artifact_size_bytes: int
        runtime_qualification: dict
        inference_runtime: None = None
        output_contract: None = None

        @model_validator(mode="after")
        def validate_identity(self):
            validate_receipt(self.model_dump(mode="json"))
            parsed = urlsplit(self.download_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if (parsed.username or parsed.password or parsed.fragment or not parsed.hostname
                or origin not in origins):
                raise ValueError("Artifact URL origin is not approved")
            return self

    return InstallRequest


def inspect(path, receipt):
    from libreyolo_native import LibreDetectionModel
    model = LibreDetectionModel(path, receipt, device="cpu")
    model.close()


def validate_installed_receipt(receipt, path):
    validate_receipt(receipt)
    if path.stat().st_size != receipt.get("artifact_size_bytes"):
        raise ValueError("Installed native artifact size differs from receipt")
