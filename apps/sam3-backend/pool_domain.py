"""One lifecycle domain over SAM3's image, multiplex, and PVS pools."""

from __future__ import annotations

from typing import Any, Literal

from aap_backend_runtime import ManagedLruPool

PoolId = Literal["image", "multiplex_video", "pvs_video"]


class Sam3Pools:
    """Aggregate three independent roots without hiding physical residency."""

    def __init__(
        self,
        image: ManagedLruPool[Any, Any],
        multiplex: ManagedLruPool[Any, Any],
        pvs: ManagedLruPool[Any, Any],
    ) -> None:
        self.image = image
        self.multiplex = multiplex
        self.pvs = pvs

    def _pool_for(self, pool_id: PoolId) -> ManagedLruPool[Any, Any]:
        if pool_id == "image":
            return self.image
        if pool_id == "multiplex_video":
            return self.multiplex
        if pool_id == "pvs_video":
            return self.pvs
        raise ValueError(f"unsupported pool: {pool_id}")

    async def snapshot_for(self, pool_id: PoolId) -> dict[str, Any]:
        return await self._pool_for(pool_id).snapshot()

    async def snapshot(self) -> dict[str, Any]:
        pools = {
            "image": await self.image.snapshot(),
            "multiplex_video": await self.multiplex.snapshot(),
            "pvs_video": await self.pvs.snapshot(),
        }
        resident_values = tuple(snapshot["gpu_resident"] for snapshot in pools.values())
        if True in resident_values:
            gpu_resident: bool | None = True
        elif all(value is False for value in resident_values):
            gpu_resident = False
        else:
            gpu_resident = None

        devices = {
            snapshot["device"]
            for snapshot in pools.values()
            if snapshot.get("device") is not None
        }
        return {
            "cap": sum(snapshot["cap"] for snapshot in pools.values()),
            "current_size": sum(
                snapshot["current_size"] for snapshot in pools.values()
            ),
            "loaded_keys": [
                {**item, "key": f"{pool_id}:{item['key']}"}
                for pool_id, snapshot in pools.items()
                for item in snapshot["loaded_keys"]
            ],
            "last_evict": None,
            "builders": sum(snapshot["builders"] for snapshot in pools.values()),
            "reserved_build_slots": sum(
                snapshot["reserved_build_slots"] for snapshot in pools.values()
            ),
            "borrowers": sum(snapshot["borrowers"] for snapshot in pools.values()),
            "waiters": sum(snapshot["waiters"] for snapshot in pools.values()),
            "cleanup_in_progress": any(
                snapshot["cleanup_in_progress"] for snapshot in pools.values()
            ),
            "cleanup_failed": any(
                snapshot["cleanup_failed"] for snapshot in pools.values()
            ),
            "gpu_resident": gpu_resident,
            "device": next(iter(devices)) if len(devices) == 1 else None,
            "provider": None,
            "active_sessions": sum(
                snapshot.get("active_sessions", 0) for snapshot in pools.values()
            ),
            "pools": pools,
        }

    async def unload_all(
        self,
        *,
        reason: str = "manual",
        force_cleanup: bool = False,
    ) -> int:
        count = 0
        errors: list[BaseException] = []
        for pool in (self.image, self.multiplex, self.pvs):
            try:
                count += await pool.unload_all(
                    reason=reason,
                    force_cleanup=force_cleanup,
                )
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
        return count

    async def unload_legacy_all(self) -> tuple[bool, bool, bool]:
        count = await self.unload_all(reason="manual", force_cleanup=True)
        snapshot = await self.snapshot()
        image_loaded = snapshot["pools"]["image"]["current_size"] > 0
        video_loaded = any(
            snapshot["pools"][pool_id]["current_size"] > 0
            for pool_id in ("multiplex_video", "pvs_video")
        )
        return count > 0, image_loaded, video_loaded

    async def unload_idle(self, pool_id: PoolId, *, idle_before: float) -> int:
        return await self._pool_for(pool_id).unload_idle(idle_before=idle_before)

    async def shutdown(self) -> None:
        errors: list[BaseException] = []
        for pool in (self.image, self.multiplex, self.pvs):
            try:
                await pool.shutdown()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


__all__ = ["PoolId", "Sam3Pools"]
