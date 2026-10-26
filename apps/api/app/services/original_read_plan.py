"""Immutable physical routing facts, separate from logical original ownership."""

from dataclasses import dataclass
import uuid

from app.services.managed_original_reader import ObjectSpec


@dataclass(frozen=True)
class ReadRoute:
    publication_id: uuid.UUID
    publication_revision: int
    source_object_id: str
    source_location_id: uuid.UUID
    source_endpoint_url: str
    source_bucket: str
    source_object_key: str
    source_version_id: str | None
    sha256: str
    size_bytes: int
    source_etag: str
    is_latest: bool
    target_location_id: uuid.UUID
    target_location_revision: int
    target_connection_id: uuid.UUID
    target_connection_revision: int
    target_endpoint_url: str
    target_bucket: str
    target_object_key: str
    target_version_id: str
    target_etag: str
    target_content_sha256: str
    creation_nonce: str
    creation_plan_uuid: str
    maintenance_revision: int

    @property
    def purpose(self):
        return f"migration_read_{self.publication_id.hex}"


@dataclass(frozen=True)
class PhysicalReadPlan:
    location_id: uuid.UUID | None
    maintenance_revision: int | None
    spec: ObjectSpec
    read_route: ReadRoute | None = None

    def purpose(self, existing_label):
        return (
            self.read_route.purpose if self.read_route is not None else existing_label
        )
