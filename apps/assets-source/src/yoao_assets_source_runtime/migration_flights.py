"""Remote handles prevent permit settlement until the Source process closes them."""

import asyncio
from contextlib import asynccontextmanager
import fcntl
import os
import stat
from uuid import UUID, uuid4, uuid5

from yoao_assets_source.saved_io import SavedIOError

from .saved_io_storage import Download

_OPEN = "yoao_assets_v1.migration_io_open_flight"
_CLOSE = "yoao_assets_v1.migration_io_close_flight"
_RECONCILE = "yoao_assets_v1.migration_io_reconcile_flights"


def _lock_spool(path):
    directory = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    try:
        root = os.fstat(directory)
        if root.st_uid != os.geteuid() or root.st_mode & 0o022:
            raise ValueError
        fd = os.open(
            ".yoao-migration-flights",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise ValueError
        raw = os.pread(fd, 17, 0)
        if not raw:
            raw = uuid4().bytes
            if os.pwrite(fd, raw, 0) != len(raw):
                raise OSError
            os.fsync(fd)
            os.fsync(directory)
        return uuid5(UUID(bytes=raw), f"{info.st_dev}:{info.st_ino}"), fd
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        raise RuntimeError(
            "Migration requires its exclusive, persistent Source spool"
        ) from None
    finally:
        os.close(directory)


class MigrationFlights:
    def __init__(self, registry):
        for signature in (
            f"{_OPEN}(uuid,uuid,text)",
            f"{_CLOSE}(uuid,uuid)",
            f"{_RECONCILE}(uuid)",
        ):
            registry.allow(signature)
        registry.lifespan(self.lifespan)
        self.spool_owner = None

    @asynccontextmanager
    async def lifespan(self, context):
        owner, fd = await context.run(
            _lock_spool,
            context.config.saved_io.spool_dir,
            _discard=lambda opened: os.close(opened[1]),
        )
        try:
            await context.fetch(_RECONCILE, owner)
            self.spool_owner = owner
            yield
        finally:
            self.spool_owner = None
            os.close(fd)

    async def run(self, context, operation, kind, call):
        owner = self.spool_owner
        flight = await context.fetch(_OPEN, owner, operation, kind)
        if not isinstance(flight, UUID):
            raise SavedIOError("migration_io_unproven")

        async def close():
            try:
                if await context.fetch(_CLOSE, owner, flight) is True:
                    return
            except Exception:
                pass
            raise SavedIOError(
                "source_close_unsettled", status_code=503, unsettled=True
            )

        try:
            result = await call()
        except BaseException as error:
            if not isinstance(error, asyncio.CancelledError) and not getattr(
                error, "unsettled", False
            ):
                await close()
            raise
        if isinstance(result, Download):
            result.after_close = close
        else:
            await close()
        return result
