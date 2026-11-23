"""Provider fault-injection seam for existing savepoint-scoped reference unit tests."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from app.db.models.dataset_source import DatasetScanEntry, DatasetScanRun, DatasetSource
from app.services import content_snapshot, reference_storage, storage_access
from yoao_assets_source.reference_protocol import ReferenceError
from yoao_assets_source_runtime.reference_readers import S3Reader, probe


@contextmanager
def _map_provider_failure():
    try:
        yield
    except ReferenceError as exc:
        raise reference_storage.error(exc) from None


@dataclass(frozen=True)
class ProviderReference(reference_storage.RegisteredReference):
    reader: object = field(compare=False)

    def list_page(self, relative, cursor=None):
        with _map_provider_failure():
            return self.reader.list_page(relative, cursor)

    def scan_directory(self, path, *, expected_anchor=None, expected_identity=None):
        with _map_provider_failure():
            if expected_anchor is not None and expected_anchor != self.anchor:
                raise ReferenceError("root_identity_changed")
            if expected_identity is not None and expected_identity != ["sftp", path]:
                raise ReferenceError("directory_identity_changed")
            return self.reader.scan_directory(path)

    @contextmanager
    def content_file(self, base, path, *, expected=None):
        with (
            _map_provider_failure(),
            self.reader.content_file(base, path, expected=expected) as result,
        ):
            yield result

    async def command(
        self,
        db,
        action,
        request,
        *,
        maintenance_revision,
        node_id,
        timeout=None,
        **authority,
    ):
        source = (
            await db.get(DatasetSource, UUID(request["source_id"]))
            if request.get("source_id")
            else None
        )
        base = source.config["relative_path"] if source else ""
        path = request.get("path")
        expected = None
        if action == "probe" and request.get("path_digest"):
            entry = await db.get(
                DatasetScanEntry, (UUID(request["run_id"]), request["path_digest"])
            )
            path = entry.relative_path
            expected = (
                entry.verified_content["observation"]
                if entry.verified_content
                else entry.observed
            )
        if action == "scan":
            run_id = UUID(request["run_id"])
            run = await db.get(DatasetScanRun, run_id)
            source = await db.get(DatasetSource, run.source_id)
            base = source.config["relative_path"]
            if isinstance(self.reader, S3Reader):
                from app.services.dataset_scans import path_digest

                entry = await db.get(
                    DatasetScanEntry, (run_id, path_digest("")), populate_existing=True
                )
                checkpoint = entry.observed.get("checkpoint", {})
                path, cursor = base, checkpoint.get("cursor")
            else:
                entry = await db.get(DatasetScanEntry, (run_id, request["path_digest"]))
                path = "/".join(filter(None, (base, entry.relative_path)))

        def physical():
            with _map_provider_failure():
                if action == "probe":
                    return probe(self.reader, base, path, expected)
                if action == "browse":
                    if isinstance(self.reader, S3Reader):
                        return self.reader.list_page(
                            path, request.get("cursor"), directories=True
                        )
                    return self.reader.browse_directory(
                        path,
                        request["limit"],
                        request.get("after"),
                        request.get("snapshot"),
                    )
                if isinstance(self.reader, S3Reader):
                    return self.reader.list_page(
                        base if action == "directory" else path,
                        None if action == "directory" else cursor,
                    )
                return self.reader.scan_directory(
                    base if action == "directory" else path
                )

        return await storage_access.run(
            db,
            self.location_id,
            "registration_directory" if action == "directory" else action,
            physical,
            expected_revision=maintenance_revision,
            node_id=node_id,
            timeout=timeout,
        )

    async def capture(
        self,
        db,
        action,
        request,
        expected_sha256,
        *,
        maintenance_revision,
        node_id,
        on_acquired,
        inspect_quality=False,
        **authority,
    ):
        run_id = UUID(request["run_id"])
        run = await db.get(DatasetScanRun, run_id)
        entry = await db.get(
            DatasetScanEntry, (run_id, request["path_digest"]), populate_existing=True
        )
        expected = (
            entry.verified_content["observation"]
            if entry.verified_content
            else entry.observed
        )
        if request.get("refresh") and not entry.verified_content:
            expected = None
        base, path = run.snapshot["config"]["relative_path"], entry.relative_path
        return await content_snapshot.capture_from_location(
            db,
            self.location_id,
            lambda: self.content_file(base, path, expected=expected),
            expected_sha256,
            expected_revision=maintenance_revision,
            node_id=node_id,
            purpose="scan_content"
            if action == "observation"
            else "quality_read"
            if inspect_quality
            else "reference_read",
            inspect_quality=inspect_quality,
            on_acquired=on_acquired,
        )


def use_provider(monkeypatch, location_id, reader_factory):
    base = reference_storage.configuration

    async def configure(db, location):
        access = await base(db, location)
        if location.id != location_id:
            return access
        return ProviderReference(
            access.location_id,
            access.location_revision,
            access.connection_revision,
            access.anchor,
            reader_factory(),
        )

    monkeypatch.setattr(reference_storage, "configuration", configure)
