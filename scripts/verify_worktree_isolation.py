"""Explicit live test: creates and deletes only random owned local resources.

Run with the current checkout's API Python. Not collected by the fast test glob.
"""

import asyncio
import importlib.util

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))


class LiveIsolationTests(unittest.TestCase):
    def test_autovacuum_does_not_hide_real_clients_or_block_database_drop(self):
        from app.config import settings
        from sqlalchemy import event
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool
        from validate_migrations import database_state
        from worktree_env import WorktreeError, load_identity, topology
        from worktree_resources import LocalResources

        with tempfile.TemporaryDirectory(
            prefix="aap-isolation-autovacuum-"
        ) as temporary:
            root = Path(temporary)
            backend = LocalResources(
                topology(root, load_identity(root, create=True), "test"),
                settings,
                code_root=ROOT,
            )
            asyncio.run(backend.database("create"))

            def engine_with_autovacuum(*args, **kwargs):
                engine = create_async_engine(*args, **kwargs)

                @event.listens_for(engine.sync_engine, "connect")
                def add_autovacuum(connection, _record):
                    # Inject a maintenance row without waiting for server scheduling.
                    cursor = connection.cursor()
                    try:
                        cursor.execute(
                            "CREATE TEMP VIEW pg_stat_activity AS "
                            "SELECT datname, backend_type FROM pg_catalog.pg_stat_activity "
                            "UNION ALL SELECT datname, 'autovacuum worker' FROM pg_database"
                        )
                    finally:
                        cursor.close()

                return engine

            async def probe():
                anchor = SimpleNamespace(url=backend.url)
                self.assertEqual((await backend.database())["sessions"], 0)
                self.assertEqual(
                    (await database_state(anchor, backend.resources["database"]))[2], 0
                )
                engine = create_async_engine(backend.url, poolclass=NullPool)
                try:
                    async with engine.connect():
                        self.assertEqual((await backend.database())["sessions"], 1)
                        self.assertEqual(
                            (
                                await database_state(
                                    anchor, backend.resources["database"]
                                )
                            )[2],
                            1,
                        )
                        with self.assertRaisesRegex(WorktreeError, "数据库仍有连接"):
                            await backend.database("drop")
                finally:
                    await engine.dispose()
                await backend.database("drop")
                self.assertIsNone(await backend.database())

            try:
                with (
                    patch(
                        "worktree_resources.create_async_engine",
                        side_effect=engine_with_autovacuum,
                    ),
                    patch(
                        "validate_migrations.create_async_engine",
                        side_effect=engine_with_autovacuum,
                    ),
                ):
                    asyncio.run(probe())
            finally:
                asyncio.run(backend.database("drop"))

    def test_owned_database_redis_and_buckets_are_isolated_and_rebuildable(self):
        self.assertIsNotNone(
            importlib.util.find_spec("worktree_resources"),
            "Missing real owned-resource implementation",
        )
        from app.config import settings
        from worktree_env import load_identity, topology
        from worktree_resources import LocalResources
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        import redis

        with tempfile.TemporaryDirectory(
            prefix="aap-isolation-acceptance-"
        ) as temporary:
            roots = [Path(temporary) / name for name in ("a", "b")]
            for root in roots:
                root.mkdir()
            backends = [
                LocalResources(
                    topology(root, load_identity(root, create=True), "test"),
                    settings,
                    code_root=ROOT,
                )
                for root in roots
            ]
            try:
                for backend in backends:
                    backend.prepare(skip_migrations=False)
                    backend.prepare(
                        skip_migrations=True
                    )  # idempotency and exact-head guard
                first, second = backends
                envs = [backend.environment() for backend in backends]
                self.assertNotEqual(envs[0]["DATABASE_URL"], envs[1]["DATABASE_URL"])

                async def database_probe():
                    for index, env in enumerate(envs):
                        engine = create_async_engine(env["MIGRATION_DATABASE_URL"])
                        try:
                            async with engine.begin() as connection:
                                await connection.execute(
                                    text("CREATE TABLE isolation_probe (value text)")
                                )
                                await connection.execute(
                                    text("INSERT INTO isolation_probe VALUES (:value)"),
                                    {"value": str(index)},
                                )
                                self.assertEqual(
                                    await connection.scalar(
                                        text("SELECT value FROM isolation_probe")
                                    ),
                                    str(index),
                                )
                        finally:
                            await engine.dispose()

                asyncio.run(database_probe())

                clients = [
                    redis.Redis.from_url(env["REDIS_URL"], socket_timeout=2)
                    for env in envs
                ]
                try:
                    clients[0].set("isolation-probe", "a")
                    self.assertIsNone(clients[1].get("isolation-probe"))
                    with clients[1].pubsub() as subscriber:
                        subscriber.subscribe("same-project-channel")
                        subscriber.get_message(timeout=2)
                        clients[0].publish("same-project-channel", "must-not-cross")
                        self.assertIsNone(subscriber.get_message(timeout=0.2))
                    first.stop_redis()
                    first.start_redis()
                    with redis.Redis.from_url(
                        first.environment()["REDIS_URL"]
                    ) as restarted:
                        self.assertEqual(restarted.get("isolation-probe"), b"a")
                    for backend in backends:
                        backend.s3.put_object(
                            Bucket=backend.resources["buckets"]["MINIO_BUCKET"],
                            Key="probe",
                            Body=backend.resources["id"].encode(),
                        )
                    for backend in backends:
                        response = backend.s3.get_object(
                            Bucket=backend.resources["buckets"]["MINIO_BUCKET"],
                            Key="probe",
                        )
                        self.assertEqual(
                            response["Body"].read().decode(), backend.resources["id"]
                        )
                        response["Body"].close()
                    first.destroy(first.resources["confirmation"])
                    self.assertTrue(second.inspect()["database"])
                    self.assertEqual(clients[1].ping(), True)
                    first.prepare(skip_migrations=False)
                finally:
                    for client in clients:
                        client.close()
            finally:
                # Confirm exact identities generated by this test. Foreign resources still fail closed.
                for backend in backends:
                    if getattr(backend, "touched", False):
                        backend.destroy(backend.resources["confirmation"])


if __name__ == "__main__":
    unittest.main()
