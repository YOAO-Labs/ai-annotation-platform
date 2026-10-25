#!/usr/bin/env python3
"""Diagnose Docker Desktop host-route TCP tuple reuse without changing the host."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.aap.acceptance"
LABEL_VALUE = "runtime-host-forwarding-verifier"
ID_LABEL = "org.aap.acceptance-id"
PORT = 8123
SOURCE_PORT = 22000
PAIRS = 4
MAX_TRACE_LINES = 2048
WATCHDOG = r"""
use POSIX qw(:sys_wait_h);
my $seconds = shift @ARGV;
my $program = shift @ARGV;
my $pid = fork();
die "fork failed" unless defined $pid;
if (!$pid) { exec {$program} $program, @ARGV; exit 127; }
my $deadline = time() + $seconds;
while (time() < $deadline) {
    my $done = waitpid($pid, WNOHANG);
    exit 0 if $done == $pid;
    select undef, undef, undef, 0.1;
}
if (waitpid($pid, WNOHANG) == 0) { kill 'TERM', $pid; waitpid($pid, 0); exit 124; }
exit 0;
"""

SERVER = r"""
import asyncio, sys
marker = sys.argv[1].encode()
async def handle(reader, writer):
    try:
        writer.write(marker)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8123, backlog=64)
    print("owned-route-ready", flush=True)
    async with server:
        await server.serve_forever()
asyncio.run(main())
"""

CLIENT = r"""
import asyncio, collections, json, os, resource, socket, struct, sys, time
data = json.load(sys.stdin)
def endpoint(value):
    address, port = value.split(":")
    return socket.inet_ntoa(struct.pack("<I", int(address, 16))), int(port, 16)
def rows():
    with open("/proc/net/tcp", encoding="ascii") as stream:
        return [{"local": endpoint(f[1]), "remote": endpoint(f[2]), "state": f[3]}
                for f in (line.split() for line in stream.read().splitlines()[1:])]
def states(port, peer):
    return [r["state"] for r in rows() if r["local"][1] == port and r["remote"] == tuple(peer)]
def resources():
    counts = collections.Counter(r["state"] for r in rows())
    with open("/proc/sys/net/ipv4/ip_local_port_range", encoding="ascii") as stream:
        ports = stream.read().strip()
    return {"fds": len(os.listdir("/proc/self/fd")),
            "fd_limit": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "tcp_states": dict(counts), "ephemeral_range": ports}
async def resolve(host, port):
    rows = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    addresses = sorted({(r[4][0], r[4][1]) for r in rows})
    if not addresses:
        raise RuntimeError("route has no IPv4 address")
    return addresses
async def one(route, port, iteration):
    loop, peer = asyncio.get_running_loop(), route["address"]
    record = {"route": route["name"], "iteration": iteration,
              "source_port": port, "destination": peer,
              "started_at_ns": time.time_ns(), "resources_before": resources(),
              "states_before": states(port, peer), "stage": "bind"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        record["local_address"] = sock.getsockname()
        record["stage"] = "connect"
        async with asyncio.timeout(5):
            await loop.sock_connect(sock, peer)
            record["local_address"] = sock.getsockname()
            record["peer_address"] = sock.getpeername()
            record["stage"] = "marker"
            expected, body = route["marker"].encode(), bytearray()
            while len(body) < len(expected):
                chunk = await loop.sock_recv(sock, len(expected) - len(body))
                if not chunk:
                    raise ConnectionError("endpoint closed before marker")
                body.extend(chunk)
            if bytes(body) != expected:
                raise ConnectionError("owned endpoint marker mismatch")
            record["marker_verified"] = True
            record["stage"] = "peer_fin"
            if await loop.sock_recv(sock, 1) != b"":
                raise ConnectionError("unexpected bytes after marker")
            record["peer_fin"] = True
        record["success"] = True
    except Exception as error:
        record.update(success=False, error_type=type(error).__name__,
                      errno=getattr(error, "errno", None), error=str(error)[:160])
    finally:
        sock.close()
        deadline = time.monotonic() + 2
        state = states(port, peer)
        while state and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            state = states(port, peer)
        record.update(elapsed_seconds=round(
            (time.time_ns()-record["started_at_ns"])/1e9, 6),
                      states_after_close=state, tuple_gone_after_close=not state,
                      resources_after=resources())
    return record
async def main():
    direct = await resolve(data["server_name"], 8123)
    host = await resolve("host.docker.internal", data["native_port"])
    if direct[0][0] != data["server_ip"]:
        raise RuntimeError("private DNS did not resolve to the owned server")
    routes = {
        "private": {"name": "container-direct", "address": direct[0],
                    "marker": data["server_marker"]},
        "host": {"name": "host-gateway-native-listener", "address": host[0],
                 "marker": data["native_marker"]},
    }
    pairs = []
    for index in range(data["pair_count"]):
        port = data["source_port"] + index
        private = [await one(routes["private"], port, n) for n in (1, 2)]
        forwarded = [await one(routes["host"], port, n) for n in (1, 2)]
        pairs.append({"pair": index + 1, "source_port": port,
                      "private": private, "host": forwarded})
    print(json.dumps({"resolved": {"private": direct, "host": host},
                      "pairs": pairs, "uid": os.getuid(),
                      "packet_payloads_persisted": False}))
asyncio.run(main())
"""


class VerificationError(RuntimeError):
    pass


def validate_image_id(value: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise VerificationError("--image must be an immutable sha256 image ID")
    return value


def resolve_output_path(value: str, root: Path) -> Path:
    worktree = root / ".worktree"
    if worktree.is_symlink():
        raise VerificationError("repository .worktree must not be a symlink")
    worktree.mkdir(mode=0o700, exist_ok=True)
    if not worktree.is_dir():
        raise VerificationError("repository .worktree must be a directory")
    output = Path(value)
    if not output.is_absolute():
        output = root / output
    if output.is_symlink() or output.exists():
        raise VerificationError("--output must be a new, non-symlink receipt")
    if (
        Path(os.path.abspath(output.parent)) != Path(os.path.abspath(worktree))
        or output.parent.resolve() != worktree.resolve()
    ):
        raise VerificationError("--output must be directly inside repository .worktree")
    if output.suffix != ".json":
        raise VerificationError("--output must name a .json receipt")
    return output


def labels_belong_to_task(labels: dict[str, str] | None, identity: str) -> bool:
    return bool(
        labels and labels.get(LABEL) == LABEL_VALUE and labels.get(ID_LABEL) == identity
    )


def tcpdump_argv_matches(argv: list[str], port: int, user: str) -> bool:
    expected = [
        "-i",
        "lo0",
        "-nn",
        "-tt",
        "-l",
        "-s",
        "128",
        "-Z",
        user,
        "tcp",
        "and",
        "port",
        str(port),
    ]
    return bool(argv and Path(argv[0]).name == "tcpdump" and argv[1:] == expected)


def docker(*arguments: str, timeout: int = 40) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise VerificationError("owned Docker operation timed out") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VerificationError(
            f"owned Docker operation failed: {detail[-1][:200] if detail else 'unknown'}"
        )
    return result.stdout.strip()


def inspect_optional(kind: str, name: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["docker", kind, "inspect", name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).lower()
        if "no such" in message or "not found" in message:
            return None
        raise VerificationError(f"could not inspect owned {kind}")
    rows = json.loads(result.stdout)
    return rows[0] if len(rows) == 1 else None


def resource_by_name(kind: str, name: str, identity: str) -> dict[str, Any] | None:
    record = inspect_optional(kind, name)
    if record is None:
        return None
    labels = (
        record.get("Config", {}).get("Labels")
        if kind == "container"
        else record.get("Labels")
    )
    if not labels_belong_to_task(labels, identity):
        raise VerificationError(f"refusing to touch unowned Docker {kind}")
    return record


def verify_docker_target() -> None:
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = os.environ.get("DOCKER_HOST")
    if context:
        record = json.loads(docker("context", "inspect", context, timeout=10))[0]
        endpoint = record.get("Endpoints", {}).get("docker", {}).get("Host", "")
    elif not endpoint:
        context = docker("context", "show", timeout=10)
        record = json.loads(docker("context", "inspect", context, timeout=10))[0]
        endpoint = record.get("Endpoints", {}).get("docker", {}).get("Host", "")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "unix" or not parsed.path.startswith("/"):
        raise VerificationError("effective Docker endpoint must be a local Unix socket")
    operating_system = docker("info", "--format", "{{.OperatingSystem}}", timeout=15)
    if "Docker Desktop" not in operating_system:
        raise VerificationError("Docker endpoint is not Docker Desktop")


def tcpdump_child(parent: int, port: int, user: str) -> tuple[int, int] | None:
    result = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,ppid=,uid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise VerificationError("could not verify the owned tcpdump process")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            try:
                pid, ppid, uid = map(int, fields[:3])
                processes[pid] = {"parent": ppid, "uid": uid, "command": fields[3]}
            except ValueError:
                continue
    matches = []
    for pid, row in processes.items():
        try:
            argv = shlex.split(row["command"])
        except ValueError:
            continue
        ancestor = row["parent"]
        for _ in range(6):
            if ancestor == parent:
                if tcpdump_argv_matches(argv, port, user):
                    matches.append((pid, row["uid"]))
                break
            ancestor_row = processes.get(ancestor)
            if ancestor_row is None or ancestor_row["parent"] == ancestor:
                break
            ancestor = ancestor_row["parent"]
    return matches[0] if len(matches) == 1 else None


class HostTrace:
    def __init__(self, port: int) -> None:
        if sys.platform != "darwin" or os.getuid() == 0:
            raise VerificationError("--host-trace requires an ordinary macOS user")
        if not Path("/usr/bin/perl").is_file():
            raise VerificationError("--host-trace requires stock /usr/bin/perl")
        self.port = port
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.lines: list[str] = []
        self.truncated = False
        self.ready = threading.Event()
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.capture_pid: int | None = None
        self.returncode: int | None = None

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            line = line.rstrip()[:400]
            if "listening on lo0" in line:
                self.ready.set()
            if len(self.lines) < MAX_TRACE_LINES:
                self.lines.append(line)
            else:
                self.truncated = True

    def start(self) -> None:
        command = [
            "sudo",
            "-n",
            "/usr/bin/perl",
            "-e",
            WATCHDOG,
            "45",
            "/usr/sbin/tcpdump",
            "-i",
            "lo0",
            "-nn",
            "-tt",
            "-l",
            "-s",
            "128",
            "-Z",
            self.user,
            "tcp",
            "and",
            "port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        if not self.ready.wait(5) or self.process.poll() is not None:
            self.stop()
            raise VerificationError(
                "host trace prerequisite failed; run sudo -v before --host-trace"
            )
        capture = tcpdump_child(self.process.pid, self.port, self.user)
        if capture is None:
            self.stop()
            raise VerificationError("host trace tcpdump child could not be verified")
        self.capture_pid, uid = capture
        if uid != os.getuid():
            self.stop()
            raise VerificationError("tcpdump did not drop to the current user")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            capture = tcpdump_child(self.process.pid, self.port, self.user)
            if capture is None:
                try:
                    self.returncode = self.process.wait(timeout=46)
                except subprocess.TimeoutExpired as error:
                    raise VerificationError(
                        "owned tcpdump watchdog did not stop within its bound"
                    ) from error
                if self.returncode == 124:
                    raise VerificationError("host trace reached its 45-second watchdog")
                capture = None
            if capture is None:
                if self.reader is not None:
                    self.reader.join(timeout=2)
                self.process = None
                return
            pid, uid = capture
            if uid == os.getuid():
                os.kill(pid, signal.SIGINT)
            else:
                stopped = subprocess.run(
                    ["sudo", "-n", "/bin/kill", "-INT", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if stopped.returncode:
                    raise VerificationError("sudo -n could not signal verified tcpdump")
        try:
            self.returncode = self.process.wait(timeout=46)
        except subprocess.TimeoutExpired as error:
            raise VerificationError(
                "owned tcpdump watchdog did not stop within its bound"
            ) from error
        if self.returncode == 124:
            raise VerificationError("host trace reached its 45-second watchdog")
        if self.reader is not None:
            self.reader.join(timeout=2)
        self.process = None


async def host_control(port: int, marker: bytes) -> dict[str, Any]:
    started = time.time_ns()
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            peer = writer.get_extra_info("peername")
            if await reader.readexactly(len(marker)) != marker:
                raise ConnectionError("native listener marker mismatch")
            if await reader.read(1) != b"":
                raise ConnectionError("native listener did not send FIN")
        return {"started_at_ns": started, "success": True, "peer": peer}
    except Exception as error:
        return {
            "started_at_ns": started,
            "success": False,
            "error_type": type(error).__name__,
            "error": str(error)[:200],
        }
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def write_receipt(path: Path, result: dict[str, Any]) -> None:
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    path.chmod(0o600)


async def verify(args: argparse.Namespace, output: Path, source_commit: str) -> int:
    identity = uuid.uuid4().hex
    name = f"aap-host-forwarding-{identity[:12]}"
    network = name + "-net"
    client = name + "-client"
    native: asyncio.AbstractServer | None = None
    trace: HostTrace | None = None
    server_id: str | None = None
    network_id: str | None = None
    network_attempted = False
    cleanup = {
        "client_removed": True,
        "server_removed": True,
        "network_removed": True,
        "errors": [],
    }
    controls: list[dict[str, Any]] = []
    peers: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "scope": "owned raw TCP same-tuple graceful-close comparison; no SQL",
        "source_commit": source_commit,
        "image_id": args.image,
        "packet_payloads_persisted": False,
        "status": "inconclusive",
        "cleanup": cleanup,
        "host_controls": controls,
        "native_host_peers": peers,
    }
    exit_code = 2
    server_marker = "server-" + uuid.uuid4().hex
    native_marker = "native-" + uuid.uuid4().hex

    async def handle_native(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        accepted = {"accepted_at_ns": time.time_ns(), "peer": peer}
        peers.append(accepted)
        try:
            writer.write(native_marker.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            accepted["closed_at_ns"] = time.time_ns()

    try:
        network_attempted = True
        network_id = docker(
            "network",
            "create",
            "--label",
            f"{LABEL}={LABEL_VALUE}",
            "--label",
            f"{ID_LABEL}={identity}",
            network,
        )
        docker(
            "run",
            "-d",
            "--no-healthcheck",
            "--name",
            name,
            "--network",
            network,
            "--label",
            f"{LABEL}={LABEL_VALUE}",
            "--label",
            f"{ID_LABEL}={identity}",
            "--read-only",
            "--user",
            "10003:10003",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "128m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--entrypoint",
            "/opt/venv/bin/python",
            args.image,
            "-I",
            "-B",
            "-u",
            "-c",
            SERVER,
            server_marker,
        )
        server = resource_by_name("container", name, identity)
        if server is None:
            raise VerificationError("owned echo server container was not created")
        server_id = server["Id"]
        server_ip = server["NetworkSettings"]["Networks"][network]["IPAddress"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = resource_by_name("container", name, identity)
            state = current.get("State", {}) if current else {}
            logs = docker("logs", server_id, timeout=10)
            if state.get("Running") and "owned-route-ready" in logs:
                break
            if not state.get("Running"):
                raise VerificationError("owned echo server failed to start")
            await asyncio.sleep(0.1)
        else:
            raise VerificationError("owned echo server readiness timed out")

        native = await asyncio.start_server(handle_native, "127.0.0.1", 0, backlog=64)
        native_port = int(native.sockets[0].getsockname()[1])
        result["owned_endpoints"] = {
            "server_container_id": server_id,
            "server_ip": server_ip,
            "private_port": PORT,
            "native_listener_port": native_port,
        }
        if args.host_trace:
            trace = HostTrace(native_port)
            trace.start()

        for _ in range(PAIRS * 2):
            controls.append(await host_control(native_port, native_marker.encode()))
        client_input = json.dumps(
            {
                "server_name": name,
                "server_ip": server_ip,
                "server_marker": server_marker,
                "native_port": native_port,
                "native_marker": native_marker,
                "pair_count": PAIRS,
                "source_port": SOURCE_PORT,
            }
        ).encode()
        command = [
            "docker",
            "run",
            "-i",
            "--no-healthcheck",
            "--name",
            client,
            "--network",
            network,
            "--label",
            f"{LABEL}={LABEL_VALUE}",
            "--label",
            f"{ID_LABEL}={identity}",
            "--read-only",
            "--user",
            "10003:10003",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--entrypoint",
            "/opt/venv/bin/python",
            args.image,
            "-I",
            "-B",
            "-u",
            "-c",
            CLIENT,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(client_input), 90
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise VerificationError("owned raw TCP client timed out") from error
        if process.returncode != 0:
            raise VerificationError(
                "owned raw client failed: " + stderr.decode(errors="replace")[-200:]
            )
        result["measurement"] = json.loads(stdout)
        result["client_stderr_tail"] = stderr.decode(errors="replace")[-300:]
        for _ in range(PAIRS * 2):
            controls.append(await host_control(native_port, native_marker.encode()))

        private = [
            attempt
            for pair in result["measurement"]["pairs"]
            for attempt in pair["private"]
        ]
        host_first = [pair["host"][0] for pair in result["measurement"]["pairs"]]
        host_second = [pair["host"][1] for pair in result["measurement"]["pairs"]]
        controls_pass = all(row["success"] for row in controls)
        private_pass = all(
            row["success"] and row["tuple_gone_after_close"] for row in private
        )
        first_pass = all(
            row["success"] and row["tuple_gone_after_close"] for row in host_first
        )
        failures = [row for row in host_second if not row["success"]]
        tuple_failures = [
            row
            for row in failures
            if row.get("stage") == "connect"
            and row.get("error_type") == "TimeoutError"
            and not row.get("states_before")
            and row.get("tuple_gone_after_close")
        ]
        unexpected_failures = [row for row in failures if row not in tuple_failures]
        result["summary"] = {
            "private_attempts": len(private),
            "private_successes": sum(row["success"] for row in private),
            "host_first_successes": sum(row["success"] for row in host_first),
            "host_second_attempts": len(host_second),
            "host_second_failures": len(failures),
            "qualified_connect_timeouts": len(tuple_failures),
            "host_control_attempts": len(controls),
            "host_control_failures": sum(not row["success"] for row in controls),
        }
        if (
            private_pass
            and first_pass
            and controls_pass
            and tuple_failures
            and not unexpected_failures
        ):
            result["host_second_failure_records"] = failures
            result["status"] = "diagnosed_not_repaired"
            exit_code = 1
        elif private_pass and first_pass and controls_pass:
            result["status"] = "passed_no_failure_reproduced"
            exit_code = 0
        else:
            result["status"] = "inconclusive_or_control_failed"
            exit_code = 1
    except VerificationError as error:
        result["error"] = str(error)
        result["status"] = "failed_prerequisite"
    except Exception as error:  # noqa: BLE001
        result["error"] = f"{type(error).__name__}: {str(error)[:200]}"
        result["status"] = "failed_prerequisite"
    finally:
        if trace is not None:
            try:
                trace.stop()
            except Exception as error:  # noqa: BLE001
                cleanup["errors"].append(f"host trace stop: {str(error)[:160]}")
            if (
                trace.returncode not in (None, 0)
                and result.get("status") != "failed_prerequisite"
            ):
                cleanup["errors"].append(f"host trace exited with {trace.returncode}")
            result["host_trace"] = {
                "lines": trace.lines,
                "truncated": trace.truncated,
                "tcpdump_pid": trace.capture_pid,
                "watchdog_returncode": trace.returncode,
                "watchdog_seconds": 45,
                "port_filter": result.get("owned_endpoints", {}).get(
                    "native_listener_port"
                ),
            }
        if native is not None:
            native.close()
            await native.wait_closed()
        for resource_name, expected_id, key in (
            (client, None, "client_removed"),
            (name, server_id, "server_removed"),
        ):
            try:
                record = resource_by_name("container", resource_name, identity)
                if record is None:
                    cleanup[key] = True
                elif expected_id is not None and record.get("Id") != expected_id:
                    raise VerificationError("owned container ID changed")
                else:
                    docker("rm", "-f", record["Id"], timeout=20)
                    cleanup[key] = True
            except Exception as error:  # noqa: BLE001
                cleanup[key] = False
                cleanup["errors"].append(f"{resource_name}: {str(error)[:160]}")
        if network_attempted:
            try:
                record = resource_by_name("network", network, identity)
                if record is None:
                    cleanup["network_removed"] = True
                elif (
                    network_id is not None and record.get("Id") != network_id
                ) or record.get("Containers"):
                    raise VerificationError("owned network changed or is not empty")
                else:
                    docker("network", "rm", network, timeout=20)
                    cleanup["network_removed"] = True
            except Exception as error:  # noqa: BLE001
                cleanup["network_removed"] = False
                cleanup["errors"].append(f"network: {str(error)[:160]}")
        if cleanup["errors"] or not all(
            cleanup[key]
            for key in ("client_removed", "server_removed", "network_removed")
        ):
            result["status"] = "cleanup_failed"
            exit_code = 2
        try:
            write_receipt(output, result)
        except Exception as error:  # noqa: BLE001
            print(
                f"receipt write failed: {type(error).__name__}: {str(error)[:160]}",
                file=sys.stderr,
            )
            exit_code = 2
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="existing local immutable sha256 image ID"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="new JSON receipt directly inside repository .worktree",
    )
    parser.add_argument(
        "--host-trace",
        action="store_true",
        help="capture bounded TCP headers on lo0 via pre-authorized sudo",
    )
    return parser.parse_args()


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    try:
        args.image = validate_image_id(args.image)
        output = resolve_output_path(args.output, ROOT)
        verify_docker_target()
        image = json.loads(docker("image", "inspect", args.image))[0]
        if image.get("Id") != args.image:
            raise VerificationError("local image ID did not match --image")
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (
        VerificationError,
        json.JSONDecodeError,
        IndexError,
        subprocess.SubprocessError,
    ) as error:
        print(f"preflight failed: {error}", file=sys.stderr)
        return 2
    try:
        status = asyncio.run(verify(args, output, source_commit))
    except KeyboardInterrupt:
        print("interrupted; owned resource cleanup was attempted", file=sys.stderr)
        return 2
    print(f"{output.relative_to(ROOT)}: {status}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
