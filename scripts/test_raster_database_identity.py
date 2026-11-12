"""Raster database deletion checks original OID and Redis under the owner lock."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import worktree_resources as resources


class Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class Connection:
    def __init__(self, case):
        self.case = case

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement, _parameters=None):
        sql = str(statement)
        if "pg_advisory_unlock" in sql:
            self.case.events.append("unlock")
            self.case.locked = False
            return Result(None)
        if "pg_advisory_lock" in sql:
            self.case.events.append("lock")
            self.case.locked = True
            return Result(None)
        if "FROM pg_database WHERE datname" in sql:
            self.case.events.append("read")
            return Result(self.case.row)
        raise AssertionError(sql)

    async def exec_driver_sql(self, sql):
        self.case.events.append(sql)
        if sql.startswith("DROP DATABASE"):
            self.case.row = None


class RasterDatabaseIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.locked = False
        self.replacement = False
        self.row = {"oid": 123, "owner": "owned", "sessions": 0}
        self.backend = resources.LocalResources.__new__(resources.LocalResources)
        self.backend.resources = {
            "database": "owned_db",
            "owner": "owned",
            "redis_name": "owned-redis",
        }
        self.backend.url = SimpleNamespace(set=lambda **_kwargs: "fake-url")

        async def verified_server(_connection):
            return None

        self.backend.verify_database_server = verified_server
        self.prepared = resources._WorktreeDestroyPreparation(self.backend, None, None)
        self.backend._destroy_preparation = self.prepared
        self.prepared.phase = "database_intent"
        self.prepared.storage = "raster_provider_retired"
        self.prepared.raster_database_oid = 123
        self.prepared.raster_redis_retired = True
        identifier = "e" * 64
        self.prepared.raster_owner = SimpleNamespace(
            data={
                "raster": {
                    "redis_birth": {
                        "container_id": identifier,
                        "pid": 70,
                        "started_at": "start",
                    },
                    "redis_retirement": {
                        "state": "retired",
                        "container_id": identifier,
                        "pid": 70,
                        "started_at": "start",
                        "finished_at": "finish",
                        "signal": "SIGTERM",
                        "signal_ack": identifier,
                        "wait_exit": 0,
                        "removed": identifier,
                        "exit_state": {
                            "status": "exited",
                            "running": False,
                            "pid": 0,
                            "exit_code": 0,
                            "oom_killed": False,
                            "error_empty": True,
                            "restart_count": 0,
                        },
                    },
                }
            }
        )
        engine = SimpleNamespace(
            connect=lambda: Connection(self),
            dialect=SimpleNamespace(
                identifier_preparer=SimpleNamespace(
                    quote_identifier=lambda value: '"' + value + '"'
                )
            ),
        )

        async def dispose():
            return None

        engine.dispose = dispose
        self.enterContext(
            patch.object(resources, "create_async_engine", return_value=engine)
        )
        self.enterContext(patch.object(resources, "docker", side_effect=self.docker))

    def docker(self, *_args):
        if not self.locked:
            raise AssertionError(
                "Redis identity check must hold database advisory lock"
            )
        self.events.append("redis-check")
        return "f" * 64 if self.replacement else ""

    def test_preparation_copies_verified_source_database_oid(self):
        self.backend.resources["root"] = "/owned"
        identity = {
            "database": "owned_db",
            "ownership": "owned",
            "resource_root": "/owned",
            "database_oid": 123,
        }
        self.prepared.fixture_owner = SimpleNamespace(
            data={"fixture_store": {"database": identity}}
        )
        self.prepared.phase = "prepared"
        self.prepared.storage = None
        self.prepared.raster_owner = None
        raster_owner = SimpleNamespace(
            data={"raster": {"resources": self.backend.resources}}
        )
        with (
            patch("fixture_store._raster_require_live_birth"),
            patch.object(self.backend, "_raster_redis_live"),
        ):
            self.backend._bind_raster_destroy_identity(self.prepared, raster_owner)
        identity["database_oid"] = 456
        self.assertEqual(self.prepared.raster_database_oid, 123)

    async def test_original_oid_and_no_sessions_drop_under_lock(self):
        await self.backend._database("drop", raster_prepared=self.prepared)
        self.assertEqual(
            self.events,
            [
                "lock",
                "redis-check",
                "redis-check",
                "read",
                'DROP DATABASE "owned_db"',
                "read",
                "unlock",
            ],
        )

    async def test_disappeared_or_replaced_database_is_not_dropped(self):
        for row in (None, {"oid": 456, "owner": "owned", "sessions": 0}):
            with self.subTest(row=row):
                self.row = row
                self.events.clear()
                with self.assertRaises(resources.WorktreeError):
                    await self.backend._database("drop", raster_prepared=self.prepared)
                self.assertNotIn('DROP DATABASE "owned_db"', self.events)
                self.assertEqual(self.events[-1], "unlock")

    async def test_active_sessions_prevent_drop(self):
        self.row["sessions"] = 1
        with self.assertRaises(resources.WorktreeError):
            await self.backend._database("drop", raster_prepared=self.prepared)
        self.assertNotIn('DROP DATABASE "owned_db"', self.events)

    async def test_redis_replacement_before_database_read_prevents_drop(self):
        self.replacement = True
        with self.assertRaises(resources.WorktreeError):
            await self.backend._database("drop", raster_prepared=self.prepared)
        self.assertEqual(self.events, ["lock", "redis-check", "unlock"])
        self.assertEqual(self.row["oid"], 123)
