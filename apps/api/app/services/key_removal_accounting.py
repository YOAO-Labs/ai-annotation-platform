"""Observe current-content changes when an existing key-only delete adds a marker."""

from app.services.export_media_removal_accounting import (
    Observation as VersionRemoval,
    Recorder as Recorder,
    finish as finish,
)
from app.services.export_zip_accounting import immutable, snapshot
from app.services.object_removal_accounting import recorder as recorder


class Observation(VersionRemoval):
    def __init__(self, accounting, client, key):
        super().__init__(accounting, client, key, None)

    def parameters(self, params, **kwargs):
        valid = (
            params.get("Bucket") == self.accounting.bucket
            and params.get("Key") == self.key
            and "VersionId" not in params
        )
        with self.lock:
            self.valid = valid
            self.opaque = self.opaque or not valid

    def _complete(self, response):
        if not (
            self.valid
            and not self.opaque
            and self.sends == 1
            and isinstance(response, dict)
            and response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (200, 204)
            and not response.get("Error")
            and response.get("DeleteMarker") is True
            and immutable(response.get("VersionId"))
        ):
            return
        version = response["VersionId"]
        self.attempt.evidence["returned_version"] = version
        before, after = (
            self.before,
            snapshot(self.client, self.accounting.bucket, self.key),
        )
        if before is None or after is None:
            return
        self.attempt.evidence.update(before=before.evidence, after=after.evidence)
        marker = after.versions.get(version)
        if (
            version in before.versions
            or marker is None
            or marker[:2] != ("marker", version)
            or after.current != ("marker", version)
            or after.versions != {**before.versions, version: marker}
        ):
            return
        self.attempt.evidence.update(
            delete_marker_created=True, current_delta_bytes=after.size - before.size
        )
