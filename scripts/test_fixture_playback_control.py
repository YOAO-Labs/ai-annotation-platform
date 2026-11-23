import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fixture_playback_control import (
    PlaybackControlError,
    bind_playback_control,
    process_start,
    retire_playback_control,
    revoke_owned_playback,
)
from worktree_env import atomic_json

_PROGRAM = """
import asyncio
import os
import signal
import socket
import sys
from uuid import UUID

from fixture_playback_control import serve_playback_control

class Registry:
    async def revoke_contexts(self, identity):
        tasks, datasets = identity
        task_ids = ",".join(map(str, sorted(tasks)))
        dataset_ids = ",".join(map(str, sorted(datasets)))
        print(f"{task_ids};{dataset_ids}", flush=True)
        if await asyncio.to_thread(os.read, 0, 1) != b"y":
            raise RuntimeError("controlled lost acknowledgement")

async def main():
    fd, suite, verifier = sys.argv[1:]
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    async with serve_playback_control(
        socket.socket(fileno=int(fd)), UUID(suite), verifier, Registry(), lambda *ids: ids
    ):
        print("READY", flush=True)
        await stopped.wait()

asyncio.run(main())
"""


@unittest.skipUnless(
    sys.platform == "linux", "native control uses Linux peer credentials"
)
class PlaybackControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.directory = Path(self.workspace.name) / ("long-suite-path-" * 9)
        self.directory.mkdir(mode=0o700)
        self.suite = uuid4()
        self.owner = {"pid": os.getpid(), "start": process_start(os.getpid())}

    def records(self, *, mode, api_owned, api_pid=None):
        resources = {"mode": mode}
        nonce = secrets.token_hex(32)
        atomic_json(
            self.directory / "owner.json",
            {**self.owner, "resources": resources, "nonce": nonce},
        )
        atomic_json(
            self.directory / "generation.json",
            {"resources": resources, "nonce": nonce, "state": "running"},
        )
        supervisor = {
            **self.owner,
            "schema": "aap-fixture-native-supervisor/v1",
            "suite_id": str(self.suite),
            "resources": resources,
            "api_owned": api_owned,
        }
        if api_pid is not None:
            supervisor["api"] = {"pid": api_pid, "start": process_start(api_pid)}
        atomic_json(self.directory / "native-supervisor.json", supervisor)

    async def test_only_verified_non_api_test_topology_skips_control(self):
        self.records(mode="test", api_owned=False)
        await revoke_owned_playback(self.directory, self.suite, (), ())
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, uuid4(), (), ())
        self.records(mode="e2e", api_owned=False)
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, self.suite, (), ())
        self.records(mode="test", api_owned=True)
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, self.suite, (), ())

    async def test_inherited_socket_uses_api_peer_and_requires_exact_ack(self):
        endpoint, verifier = bind_playback_control(self.directory, self.suite)
        with endpoint:
            peer = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _PROGRAM,
                str(endpoint.fileno()),
                str(self.suite),
                verifier,
                cwd=Path(__file__).parent,
                pass_fds=(endpoint.fileno(),),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        self.addAsyncCleanup(self.stop_peer, peer)
        self.assertEqual(await asyncio.wait_for(peer.stdout.readline(), 5), b"READY\n")
        self.records(mode="e2e", api_owned=True, api_pid=os.getpid())
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, self.suite, (), ())
        self.records(mode="e2e", api_owned=True, api_pid=peer.pid)
        capability_path = self.directory / "playback-control.json"
        capability = json.loads(capability_path.read_text())
        atomic_json(capability_path, {**capability, "token": secrets.token_urlsafe(32)})
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, self.suite, (), ())
        atomic_json(capability_path, capability)

        task, dataset = uuid4(), uuid4()
        for result in (b"n", b"y"):
            request = asyncio.create_task(
                revoke_owned_playback(self.directory, self.suite, (task,), (dataset,))
            )
            self.addAsyncCleanup(self.cancel_request, request)
            self.assertEqual(
                await asyncio.wait_for(peer.stdout.readline(), 5),
                f"{task};{dataset}\n".encode(),
            )
            self.assertFalse(request.done())
            peer.stdin.write(result)
            await peer.stdin.drain()
            if result == b"n":
                with self.assertRaises(PlaybackControlError):
                    await asyncio.wait_for(request, 5)
            else:
                await asyncio.wait_for(request, 5)

        peer.terminate()
        await asyncio.wait_for(peer.wait(), 5)
        self.assertEqual(peer.returncode, 0)
        with self.assertRaises(PlaybackControlError):
            await revoke_owned_playback(self.directory, self.suite, (), ())
        retire_playback_control(self.directory, self.suite)
        self.assertFalse((self.directory / "playback.sock").exists())
        self.assertFalse(capability_path.exists())

    async def test_verify_retired_requires_playback_control_files_physically_removed(
        self,
    ):
        import verify_playwright_fixture_teardown as teardown

        run = uuid4()
        resources = {"mode": "e2e"}
        nonce = secrets.token_hex(32)
        case_dir = Path(self.workspace.name) / "case"
        case_dir.mkdir(mode=0o700)
        for phase in ("teardown-entered.json", "teardown-returned.json"):
            atomic_json(case_dir / phase, {"suite_id": str(self.suite), "phase": phase})

        api_identity = {"pid": os.getpid() + 1000, "start": 101}
        control_identity = {"pid": os.getpid() + 1001, "start": 102}
        generation = {"resources": resources, "nonce": nonce, "state": "running"}
        atomic_json(
            self.directory / "generation.json", {**generation, "state": "closed"}
        )
        atomic_json(
            self.directory / "native-supervisor.json",
            {
                "schema": "aap-fixture-native-supervisor/v1",
                "suite_id": str(self.suite),
                "resources": resources,
                "state": "retired",
                "pid": self.owner["pid"],
                "start": self.owner["start"],
                "native_run_ids": [str(run)],
                "resolver_retired": True,
                "business_retired": True,
                "api_disposition": {
                    **api_identity,
                    "exit_code": 0,
                    "pidfd_exit": True,
                    "descendants_drained": True,
                    "lifespan_shutdown": True,
                },
                "control_disposition": {
                    **control_identity,
                    "exit_code": 0,
                    "pidfd_exit": True,
                    "descendants_drained": True,
                    "settled_ack": True,
                },
            },
        )
        instance = {
            "suite": self.suite,
            "run": run,
            "dir": case_dir,
            "directory": self.directory,
            "generation": generation,
            "processes": {
                "supervisor": self.owner,
                "api": api_identity,
                "control": control_identity,
            },
        }

        class _ReachedReceipts(RuntimeError):
            pass

        with patch.object(
            teardown, "_verify_run_receipts", side_effect=_ReachedReceipts
        ):
            endpoint, _ = bind_playback_control(self.directory, self.suite)
            endpoint.close()
            with self.assertRaisesRegex(RuntimeError, "^playback_control_not_retired$"):
                await teardown.verify_retired(instance, resources, None, None)

            (self.directory / "playback.sock").unlink()
            with self.assertRaisesRegex(RuntimeError, "^playback_control_not_retired$"):
                await teardown.verify_retired(instance, resources, None, None)

            (self.directory / "playback-control.json").unlink()
            (self.directory / "playback.sock").symlink_to("missing-target")
            with self.assertRaisesRegex(RuntimeError, "^playback_control_not_retired$"):
                await teardown.verify_retired(instance, resources, None, None)

            (self.directory / "playback.sock").unlink()
            (self.directory / "playback-control.json").symlink_to("missing-target")
            with self.assertRaisesRegex(RuntimeError, "^playback_control_not_retired$"):
                await teardown.verify_retired(instance, resources, None, None)

            (self.directory / "playback-control.json").unlink()
            endpoint, _ = bind_playback_control(self.directory, self.suite)
            endpoint.close()
            retire_playback_control(self.directory, self.suite)
            with self.assertRaises(_ReachedReceipts):
                await teardown.verify_retired(instance, resources, None, None)

    @staticmethod
    async def stop_peer(peer):
        if peer.returncode is None:
            peer.kill()
        await peer.wait()

    @staticmethod
    async def cancel_request(request):
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
