"""Resolve a reference backend without persisting or publishing credentials."""

from app.db.models.storage_connection import StorageConnection
from app.services import connector_guard
from app.services import filesystem_storage as fs
from app.services.sources.s3_reference import S3Access
from app.services.sources.sftp_reference import SftpAccess, qualified_root


async def configuration(db, location):
    if location.kind == "agent":
        from app.services.agent_storage import configuration as agent_configuration

        return await agent_configuration(db, location)
    if location.kind == "filesystem":
        return dict(location.config)
    if location.kind not in {"s3", "sftp"} or not location.enabled:
        raise fs.FilesystemStorageError("reference_backend_not_supported")
    connection = await db.get(
        StorageConnection, location.connection_id, populate_existing=True
    )
    if (
        connection is None
        or connection.kind != location.kind
        or not connection.secret_enc
    ):
        raise fs.FilesystemStorageError("connection_unavailable")
    from app.services.storage_connection import target_host

    try:
        await connector_guard.assert_connection_target_allowed(
            db, target_host(connection)
        )
    except connector_guard.ConnectorHostDenied:
        raise fs.FilesystemStorageError("connector_host_denied") from None
    arguments = (
        dict(connection.config),
        connection.secret_enc,
        tuple(await connector_guard.get_allowlist(db)),
    )
    if location.kind == "sftp":
        return SftpAccess(*arguments, *qualified_root(connection.config))
    return S3Access(*arguments)


def content_file(config, base, path, **kwargs):
    if isinstance(config, (S3Access, SftpAccess)):
        return config.content_file(base, path, **kwargs)
    return fs.content_file(config, base, path, **kwargs)


def sample_path(location, base, path):
    if location.kind == "s3":
        from app.services.sources.s3_reference import directory_prefix

        return directory_prefix("", base) + path
    return "/".join(p for p in [base, path] if p)
