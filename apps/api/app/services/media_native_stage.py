"""Track an output operation's exact native directory until actual removal."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from sqlalchemy import text

from app.services import storage_access


class NativeStage:
    def __init__(self, db, permit, output_kind, output_id, *, before_cleanup=None):
        self.engine = storage_access.engine(db)
        self.loop = asyncio.get_running_loop()
        self.permit_id = permit.id
        self.before_cleanup = before_cleanup
        self.location_id = permit.location_id
        self.revision = permit.revision
        root = os.path.realpath(tempfile.gettempdir())
        self.path = Path(root) / f"aap-media-stage-{permit.id}"
        self.receipt = {
            "schema": 1,
            "kind": output_kind,
            "output_id": str(output_id),
            "path": str(self.path),
            "device": None,
            "inode": None,
            "state": "prepared",
        }

    async def _checkpoint(self):
        async with self.engine.begin() as connection:
            settled = await connection.scalar(
                text(
                    "SELECT yoao_assets_storage_v1.checkpoint_media_native_stage"
                    "(:id,:location,:revision,CAST(:stage AS jsonb))"
                ),
                {
                    "stage": json.dumps(self.receipt, separators=(",", ":")),
                    "id": self.permit_id,
                    "location": self.location_id,
                    "revision": self.revision,
                },
            )
            if not settled:
                raise RuntimeError("media_native_stage_owner_changed")

    def _write(self):
        coroutine = self._checkpoint()
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException as exc:
            coroutine.close()
            raise storage_access.mark_unsettled(exc)
        try:
            pending.result()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    def __enter__(self):
        self._write()
        try:
            os.mkdir(self.path, 0o700)
            observed = os.lstat(self.path)
            if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid():
                raise RuntimeError("media_native_stage_owner_changed")
            self.receipt.update(
                state="opened", device=str(observed.st_dev), inode=str(observed.st_ino)
            )
            self._write()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        return self.path

    def __exit__(self, exc_type, failure, traceback):
        if storage_access.failure_unsettled(failure):
            return False
        try:
            observed = os.lstat(self.path)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or str(observed.st_dev) != self.receipt["device"]
                or str(observed.st_ino) != self.receipt["inode"]
            ):
                raise RuntimeError("media_native_stage_owner_changed")
            if self.before_cleanup is not None:
                self.before_cleanup()
            shutil.rmtree(self.path)
            self.receipt["state"] = "settled"
            self._write()
        except BaseException as cleanup:
            storage_access.mark_unsettled(cleanup)
            if isinstance(failure, asyncio.CancelledError):
                storage_access.mark_unsettled(failure)
                raise failure from cleanup
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "Native media work and owned stage cleanup failed",
                    [failure, cleanup],
                ) from None
            raise
        return False
