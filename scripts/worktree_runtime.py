"""Owned local resources and process lifecycle behind pnpm dev:worktree."""

from __future__ import annotations

from contextlib import contextmanager
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

from worktree_env import (
    WorktreeError,
    application_environment,
    atomic_json,
    frontend_environment,
    load_identity,
    read_state,
    state_path,
    topology,
)


def worker_queues_match(node: str, configured: str, active: list[dict]) -> bool:
    from celery.utils.nodenames import worker_direct

    expected = set(configured.split(","))
    actual = {queue["name"] for queue in active}
    # worker_direct adds the node's own inbox; unknown queues still fail closed.
    return actual in (expected, expected | {worker_direct(node).name})


def require_owner(name: str, observed: dict | None, expected: str) -> bool:
    if observed is None:
        return False
    if observed.get("owner") != expected:
        raise WorktreeError(f"{name} 已存在但不属于本环境；拒绝接管或删除")
    return True


def validate_inventory(resources: dict, inventory: dict) -> None:
    require_owner(resources["database"], inventory["database"], resources["owner"])
    require_owner(resources["redis_name"], inventory["redis"], resources["owner"])
    for name, observed in inventory["buckets"].items():
        if name not in resources["buckets"].values():
            raise WorktreeError("资源清单包含非本环境 bucket")
        require_owner(name, observed, resources["owner"])


def validate_destruction(
    resources: dict, inventory: dict, confirmation: str | None
) -> None:
    if confirmation != resources["confirmation"]:
        raise WorktreeError(
            f"此操作删除本环境数据；请提供 --confirm {resources['confirmation']}"
        )
    validate_inventory(resources, inventory)
    if (inventory["database"] or {}).get("sessions", 0):
        raise WorktreeError("数据库仍有连接；先停止本环境客户端，不会强制终止未知连接")


@contextmanager
def environment_lock(resources: dict):
    path = state_path(Path(resources["root"]), resources["mode"], "environment.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorktreeError("本环境正在使用中；请先 stop 或选择其他环境") from exc
        yield
    finally:
        os.close(
            descriptor
        )  # Keep the inode; unlinking a lock permits split ownership.


def validate_revisions(
    revisions: list[str], heads: list[str], current: list[str], *, skip: bool
) -> None:
    if len(revisions) != len(set(revisions)):
        raise WorktreeError("迁移 revision 重复；请先整合迁移历史")
    if len(heads) != 1:
        raise WorktreeError(f"迁移必须只有一个 head，当前为 {heads}")
    unknown = set(current) - set(revisions)
    if unknown:
        raise WorktreeError(
            f"数据库版本 {sorted(unknown)} 不在当前 checkout 的迁移图中；"
            "拒绝迁移/跳过。请恢复对应代码或使用确认后的独立环境 reset。"
        )
    if skip and set(current) != set(heads):
        raise WorktreeError(
            f"--skip-migrations 要求数据库已在 head {heads}，当前为 {current}"
        )


def parse_arguments(argv: list[str]):
    argv = list(argv)
    while argv and argv[0] == "--":
        argv.pop(0)
    execute = []
    if "--" in argv:
        separator = argv.index("--")
        execute, argv = argv[separator + 1 :], argv[:separator]
    parser = argparse.ArgumentParser(
        prog="pnpm dev:worktree --",
        description="独立工作树环境：共享本机 PostgreSQL/MinIO，独立数据库、Redis、bucket 和 worker。",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="up",
        choices=("up", "init", "doctor", "stop", "reset", "destroy", "exec", "recover-inference"),
    )
    parser.add_argument("--mode", choices=("dev", "test", "e2e"))
    parser.add_argument(
        "--scenario",
        choices=("filtering",),
        help="准备并保留手动验收数据，仅用于 up --mode e2e",
    )
    parser.add_argument(
        "--with-worker",
        action="store_true",
        help="启动当前 checkout 的普通及数据库维护 worker，不启动 GPU 或 beat",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="仅在数据库已经位于本 checkout head 时跳过迁移",
    )
    parser.add_argument(
        "--confirm", help="reset/destroy 要求 doctor 显示的精确环境确认值"
    )
    parser.add_argument("--api-port", type=int, default=8100)
    parser.add_argument("--web-port", type=int, default=3100)
    parser.add_argument(
        "--finish-drain",
        action="store_true",
        help="仅在原推理 owner 已完成排空且责任已结算时结束该 owner",
    )
    options = parser.parse_args(argv)
    if not all(1 <= port <= 65535 for port in (options.api_port, options.web_port)):
        parser.error("端口必须在 1–65535 范围内")
    if options.command == "exec" and not execute:
        parser.error("exec 必须使用 -- <command>")
    if options.command == "recover-inference":
        if options.finish_drain == bool(execute):
            parser.error("recover-inference 需要 --finish-drain 或 -- <command> 二选一")
    elif execute and options.command != "exec":
        parser.error("只有 exec/recover-inference 可以附带外部命令")
    elif options.finish_drain:
        parser.error("--finish-drain 只用于 recover-inference")
    if options.with_worker and options.command not in {"up", "exec"}:
        parser.error("--with-worker 只用于 up/exec")
    if options.skip_migrations and options.command not in {"up", "init", "exec"}:
        parser.error("--skip-migrations 只用于 up/init/exec")
    options.mode = options.mode or (
        "e2e" if options.command == "recover-inference"
        else "test" if options.command == "exec" else "dev"
    )
    if options.command == "recover-inference" and options.mode != "e2e":
        parser.error("recover-inference 只用于 e2e")
    if options.scenario and (options.command != "up" or options.mode != "e2e"):
        parser.error("--scenario 只用于 up --mode e2e，不写入日常开发或单元测试库")
    options.execute = execute
    return options


def process_identity(pid: int) -> dict | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    outputs = []
    for field in ("lstart=", "command="):
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", field],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode or not result.stdout.strip():
            return None
        outputs.append(result.stdout.strip())
    return {"started": outputs[0], "command": outputs[1]}


def process_handle(pid: int, scope: str) -> dict | None:
    actual = process_identity(pid)
    if not actual:
        return None
    return {
        "pid": pid,
        "started": actual["started"],
        "scope": str(Path(scope).resolve()),
        "command_hash": hashlib.sha256(actual["command"].encode()).hexdigest(),
    }


def process_matches(record: dict, required_script: str) -> bool:
    actual = process_identity(record.get("pid"))
    return bool(
        actual
        and actual["started"] == record.get("started")
        and record.get("scope") == str(Path(required_script).resolve())
        and record.get("command_hash")
        == hashlib.sha256(actual["command"].encode()).hexdigest()
    )

def capture_descendants(roots: list[int], scope: str) -> dict:
    """Record observed identities, not a claim that an escaped child is settled."""
    result = subprocess.run(
        ["ps", "-e", "-o", "pid=,ppid="],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "LC_ALL": "C"},
        check=True,
    )
    parents = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise WorktreeError("无法核对子进程树")
        pid, parent = map(int, fields)
        parents[pid] = parent
    observed = set(roots)
    while True:
        descendants = {pid for pid, parent in parents.items() if parent in observed}
        added = descendants - observed
        if not added:
            break
        observed.update(added)
    identities = []
    unidentified = []
    for pid in sorted(observed):
        handle = process_handle(pid, scope)
        if handle is None:
            unidentified.append(pid)
        else:
            identities.append({**handle, "ppid": parents.get(pid)})
    return {"identities": identities, "unidentified": unidentified}


def session_record(resources: dict) -> dict | None:
    path = state_path(Path(resources["root"]), resources["mode"], "run.json")
    if not path.exists():
        return None
    record = read_state(path)
    if record.get("owner") != resources["owner"]:
        raise WorktreeError("运行记录属于其他环境；拒绝发送信号")
    return record


def safe_error(exc: BaseException) -> str:
    # Resource/validation messages are controlled; third-party errors may expose URLs.
    if isinstance(exc, BaseExceptionGroup):
        return f"{exc.message}: " + "；".join(safe_error(item) for item in exc.exceptions)
    return (
        str(exc)
        if isinstance(exc, WorktreeError)
        else f"{type(exc).__name__}（请检查本机依赖/权限；连接凭据不输出）"
    )


def http_ready(port: int, path: str = "/") -> bool:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def diagnose(backend) -> dict:
    from worktree_resources import migration_graph

    resources = backend.resources
    report = {"initialized": True, "resources": resources, "checks": {}, "errors": []}
    checks = report["checks"]

    def check(name, operation):
        try:
            checks[name] = operation()
        except Exception as exc:
            checks[name] = None
            report["errors"].append(f"{name}: {safe_error(exc)}")

    check("database", lambda: asyncio.run(backend.database()))
    check("runtime_role", lambda: asyncio.run(backend.owned_runtime_role()))
    check("redis", backend.redis_info)
    check(
        "buckets",
        lambda: {
            name: backend.bucket_info(name) for name in resources["buckets"].values()
        },
    )
    if (
        backend.code_root / "apps/api/app/vendor/TrackEval/trackeval_metrics/metrics.py"
    ).is_file():
        check("migration_graph", lambda: migration_graph(backend.code_root))
    else:
        checks["migration_graph"] = None
        report["errors"].append("TrackEval 子模块缺失；运行 pnpm dev:worktree -- init")
    if "MINIO_INPUTS_BUCKET" in resources["buckets"]:
        check("inputs_bucket_versioning", backend.inputs_bucket_versioning)
        versioning = checks.get("inputs_bucket_versioning")
        if versioning is not None and versioning.get("Status") != "Enabled":
            report["errors"].append("独立输入 bucket 尚未启用 versioning；运行 init")
    if checks.get("database"):
        check("database_revisions", lambda: asyncio.run(backend.revisions()))
        check("runtime_login", lambda: asyncio.run(backend.verify_runtime_login()))
    else:
        report["errors"].append("独立数据库尚未创建；运行 init")
    if checks.get("buckets") is not None:
        try:
            validate_inventory(
                resources,
                {key: checks.get(key) for key in ("database", "redis", "buckets")},
            )
        except ValueError as exc:
            report["errors"].append(str(exc))
        if any(value is None for value in checks["buckets"].values()):
            report["errors"].append("部分 bucket 尚未创建")
    if checks.get("migration_graph") and checks.get("database_revisions") is not None:
        try:
            validate_revisions(
                *checks["migration_graph"], checks["database_revisions"], skip=True
            )
        except ValueError as exc:
            report["errors"].append(str(exc))
    redis_info = checks.get("redis")
    if (
        redis_info
        and redis_info["running"]
        and redis_info["owner"] == resources["owner"]
    ):

        def ping():
            import redis

            with redis.Redis(
                host="127.0.0.1", port=redis_info["port"], socket_timeout=2
            ) as client:
                return client.ping()

        check("redis_ping", ping)
    else:
        report["errors"].append("独立 Redis 未运行；init/up 会启动它")
    check("session", lambda: session_record(resources))
    record = checks["session"]
    script = str(backend.code_root / "scripts/worktree_runtime.py")
    report["running"] = bool(record and process_matches(record, script))
    report["state"] = "stopped"
    report["worker"] = "not requested"
    report["maintenance_worker"] = "not requested"
    if resources["mode"] == "e2e":
        marker_path = state_path(Path(resources["root"]), "e2e", "inference-owner.json")
        if marker_path.exists():
            def inference_status():
                marker = read_state(marker_path)
                require_owner("inference-owner", marker, resources["owner"])
                return {
                    "phase": marker.get("phase"),
                    "alive": process_matches(
                        marker,
                        str(backend.code_root / "scripts/fixture_inference_services.py"),
                    ),
                }

            check("inference_owner", inference_status)
            status = checks.get("inference_owner")
            if not status or not status["alive"] or not report["running"]:
                report["errors"].append("推理夹具 owner 尚未确认结算；保留记录")
    if report["running"]:
        report["state"] = "running"
        report["processes"] = [
            {**child, "alive": process_matches(child, str(backend.code_root))}
            for child in record.get("children", [])
        ]
        report["worker"] = record.get("worker", "not requested")
        report["maintenance_worker"] = record.get("maintenance_worker", "not requested")
        service_path = state_path(
            Path(resources["root"]), resources["mode"], "services.json"
        )
        if service_path.exists():
            check("service_record", lambda: read_state(service_path))
            services = checks["service_record"] or {}
            if services.get("supervisorPid") in [
                child["pid"] for child in report["processes"] if child["alive"]
            ]:
                report["services"] = services
                checks["api_http"] = http_ready(services.get("apiPort"), "/health/db")
                checks["web_http"] = http_ready(services.get("webPort"))
                if not checks["api_http"] or not checks["web_http"]:
                    report["errors"].append("运行中的 API/Web 未通过 HTTP 就绪检查")
        if record.get("command") == "up" and "services" not in report:
            report["state"] = "starting"
            report["errors"].append("API/Web 尚未就绪")
        for role in ("worker", "maintenance_worker"):
            if record.get("worker_requested") and not record.get(role):
                report[role] = "starting"
                report["state"] = "starting"
                report["errors"].append(f"{role} 尚未确认注册")
        if any(not child["alive"] for child in report["processes"]):
            report["errors"].append("部分子进程已经退出")
    graph = checks.get("migration_graph")
    if graph:
        checks["migration_graph"] = {"revision_count": len(graph[0]), "heads": graph[1]}
    report["healthy"] = not report["errors"]
    return report


def stop_environment(backend) -> None:
    resources = backend.resources
    record = session_record(resources)
    script = str(backend.code_root / "scripts/worktree_runtime.py")
    if record and process_matches(record, script):
        # The public Node entry point gives this owner its own process group. A direct
        # Python invocation may share the user's shell group; never signal that group.
        if os.getpgid(record["pid"]) == record["pid"]:
            os.killpg(record["pid"], signal.SIGTERM)
        else:
            os.kill(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 25
        while process_matches(record, script) and time.monotonic() < deadline:
            time.sleep(0.1)
        if process_matches(record, script):
            raise WorktreeError("环境未在 25 秒内停止；不会强制终止未确认的进程")
    with environment_lock(resources):
        record = session_record(resources)
        if record and record.get("unsettled_consumers"):
            raise WorktreeError("e2e 消费者结算未证明；保留运行记录及资源")
        if record and any(
            process_matches(child, str(backend.code_root))
            for child in record.get("children", [])
        ):
            raise WorktreeError("发现遗留子进程；请先检查 doctor，不会将它们视为已停止")
        if resources["mode"] == "e2e":
            from fixture_inference_services import FixtureInferenceServices

            FixtureInferenceServices(backend, code_root=backend.code_root).assert_stopped()
        backend.stop_redis()
    print("[dev:worktree] 本环境已停止，数据库和对象数据保留")


def reject_orphans(backend, *, allow_inference=False) -> None:
    previous = session_record(backend.resources)
    if previous and previous.get("unsettled_consumers"):
        raise WorktreeError("e2e 消费者结算未证明；保留运行记录及资源")
    if allow_inference and (not previous or not previous.get("supervisor_reaped")):
        raise WorktreeError("原监督进程没有已确认的完成回执；保留运行记录")
    if previous and any(
        process_matches(child, str(backend.code_root))
        for child in previous.get("children", [])
    ):
        raise WorktreeError("存在本环境遗留子进程；先诊断和停止，拒绝修改资源")
    if backend.resources["mode"] == "e2e" and not allow_inference:
        from fixture_inference_services import FixtureInferenceServices

        FixtureInferenceServices(backend, code_root=backend.code_root).assert_stopped()


def register_child(record: dict, handle: dict | None, role: str) -> None:
    if not handle:
        raise WorktreeError(f"{role} 的进程身份无法验证；拒绝留下未登记的运行进程")
    record["children"] = [
        child for child in record["children"] if child["pid"] != handle["pid"]
    ]
    record["children"].append({**handle, "role": role})


def run_processes(backend, options, *, fixture_inference=None) -> int:
    resources = backend.resources
    root = backend.code_root
    record_path = state_path(Path(resources["root"]), resources["mode"], "run.json")
    children = []
    stopping = False
    stop_signal = None
    inference = None
    selected_inference = fixture_inference
    exit_code = None
    canonical_e2e = resources["mode"] == "e2e" and options.command in {
        "exec", "recover-inference"
    }
    identity = process_handle(os.getpid(), str(root / "scripts/worktree_runtime.py"))
    if not identity:
        raise WorktreeError("无法核对运行进程身份；拒绝启动")
    record = {
        "owner": resources["owner"],
        **identity,
        "children": [],
        "command": options.command,
        "worker_requested": options.with_worker,
    }
    atomic_json(record_path, record)

    def retain_consumers(reason):
        if not canonical_e2e or not children or record.get("unsettled_consumers"):
            return
        # Publish the conservative fence before attempting the racy process-tree scan.
        record["unsettled_consumers"] = {"reason": reason, "snapshot": None}
        atomic_json(record_path, record)
        try:
            snapshot = capture_descendants([child.pid for child in children], str(root))
        except (OSError, ValueError, subprocess.SubprocessError):
            snapshot = {"error": "process_tree_unavailable"}
        record["unsettled_consumers"]["snapshot"] = snapshot
        atomic_json(record_path, record)

    def signal_children(sig):
        for child in children:
            if child.poll() is None:
                try:
                    if canonical_e2e:
                        os.kill(child.pid, sig)
                    else:
                        os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def request_stop(sig, _frame):
        nonlocal stopping, stop_signal
        if stopping:
            return
        stopping = True
        stop_signal = sig
        try:
            retain_consumers(f"signal_{sig}")
        finally:
            signal_children(sig)

    old_signals = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if fixture_inference is None:
            backend.prepare(skip_migrations=options.skip_migrations)
            if canonical_e2e:
                asyncio.run(backend.prepare_fixture_inference_database())
        else:
            from fixture_inference_services import FixtureInferenceServices

            if (
                not canonical_e2e
                or not isinstance(fixture_inference, FixtureInferenceServices)
                or fixture_inference.backend is not backend
                or (
                    options.command != "recover-inference"
                    and (not backend._prepared or not backend._fixture_inference_prepared)
                )
            ):
                raise WorktreeError("外部推理 owner 需要已准备的同一 e2e 环境")
        if stopping:
            return 128 + stop_signal if canonical_e2e else 0
        environment = backend.environment()
        if canonical_e2e:
            if fixture_inference is None:
                from fixture_inference_services import FixtureInferenceServices

                inference = FixtureInferenceServices(backend, code_root=root)
                inference.start()
                selected_inference = inference
            environment.update(
                selected_inference.verified_selection(resources, code_root=root)
            )
        if options.scenario:
            # Keep the manual guide readable and avoid SQL/S3 debug credential logs.
            environment = {
                key: value
                for key, value in environment.items()
                if key.upper() != "DEBUG"
            }
            environment["DEBUG"] = "false"

        def launch(
            command, label, cwd, *, child_environment=None, private_environment=None
        ):
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment if child_environment is None else child_environment,
                stdin=subprocess.PIPE if private_environment is not None else None,
                start_new_session=True,
            )
            children.append(child)
            # A signal can arrive while Popen is returning, before registration.
            if canonical_e2e and stopping:
                retain_consumers(f"signal_{stop_signal}")
                signal_children(stop_signal)
            if private_environment is not None:
                try:
                    with child.stdin as stream:
                        stream.write(json.dumps(private_environment).encode())
                except BrokenPipeError as exc:
                    raise WorktreeError(
                        "监督进程在接收 API 配置前退出；请检查前面的启动错误"
                    ) from exc
            process = process_handle(child.pid, str(root))
            if process:
                register_child(record, process, label)
                atomic_json(record_path, record)
            elif child.poll() is None:
                raise WorktreeError(f"{label} 的进程身份无法验证；停止该子进程")
            return child

        acceptance_path = None
        if options.scenario:
            seed = launch(
                [sys.executable, str(root / "scripts/worktree_filtering.py")],
                "scenario",
                root / "apps/api",
                child_environment={
                    **application_environment(environment),
                    "PLAYWRIGHT_E2E_DATABASE_URL": environment[
                        "PLAYWRIGHT_E2E_DATABASE_URL"
                    ],
                },
            )
            while seed.poll() is None and not stopping:
                time.sleep(0.1)
            if stopping:
                return 0
            if seed.returncode != 0:
                raise WorktreeError(
                    "筛选验收数据准备失败；未启动页面，请检查上面的原因"
                )
            children.remove(seed)
            record["children"] = [
                child for child in record["children"] if child["pid"] != seed.pid
            ]
            acceptance_path = str(state_path(root, "e2e", "data", "filtering.json"))
            record["scenario"] = options.scenario
            atomic_json(record_path, record)

        if options.with_worker:
            from celery import Celery

            client = Celery(
                "worktree_readiness", broker=environment["CELERY_BROKER_URL"]
            )
            try:
                for role, suffix, queues, database_url, required_task in (
                    (
                        "worker",
                        "",
                        "default,media,cleanup,audit,export,image-pyramid,ml.cpu",
                        environment["DATABASE_URL"],
                        "app.workers.audit.persist_audit_entry",
                    ),
                    (
                        "maintenance_worker",
                        "-maintenance",
                        "maintenance",
                        environment["MIGRATION_DATABASE_URL"],
                        "app.workers.audit_partition.ensure_future_audit_partitions",
                    ),
                ):
                    worker_name = (
                        f"worktree-{resources['id']}-{resources['mode']}{suffix}@local"
                    )
                    worker = launch(
                        [
                            sys.executable,
                            "-m",
                            "celery",
                            "-A",
                            "app.workers.celery_app",
                            "worker",
                            "--pool=solo",
                            "--concurrency=1",
                            "--loglevel=WARNING",
                            f"--hostname={worker_name}",
                            f"--queues={queues}",
                        ],
                        role,
                        root / "apps/api",
                        child_environment={
                            **application_environment(environment),
                            "DATABASE_URL": database_url,
                        },
                    )
                    deadline = time.monotonic() + 60
                    ready = False
                    while (
                        worker.poll() is None
                        and not stopping
                        and time.monotonic() < deadline
                    ):
                        inspector = client.control.inspect(
                            destination=[worker_name], timeout=1
                        )
                        registered = inspector.registered() or {}
                        active = inspector.active_queues() or {}
                        if required_task in registered.get(
                            worker_name, []
                        ) and worker_queues_match(
                            worker_name, queues, active.get(worker_name, [])
                        ):
                            ready = True
                            break
                    if not ready:
                        raise WorktreeError(
                            f"本工作树 {role} 未就绪；请检查注册任务与队列，不会复用共享 worker"
                        )
                    record[role] = worker_name
                    handle = process_handle(worker.pid, str(root))
                    if not handle:
                        raise WorktreeError(f"{role} 在注册后退出")
                    register_child(record, handle, role)
                    atomic_json(record_path, record)
                    print(f"[dev:worktree] {role} 已注册：{worker_name}", flush=True)
            finally:
                client.close()
        if stopping:
            return 128 + stop_signal if canonical_e2e else 0
        module = (root / "scripts/dev-worktree.mjs").as_uri()
        ports = {"apiPort": options.api_port, "webPort": options.web_port}
        if canonical_e2e:
            ports["cooperativeE2e"] = True
        if options.command in {"exec", "recover-inference"}:
            expression = f"import {{runIsolatedCommand}} from {json.dumps(module)}; process.exitCode = await runIsolatedCommand({json.dumps(options.execute)}, {json.dumps(ports)});"
            supervisor_environment = environment
            private_environment = None
        else:
            state = str(
                state_path(Path(resources["root"]), resources["mode"], "services.json")
            )
            args = [
                "--api-port",
                str(options.api_port),
                "--web-port",
                str(options.web_port),
            ]
            expression = f"import {{readFileSync}} from 'node:fs'; import {{runDevWorktree}} from {json.dumps(module)}; const apiEnvironment = JSON.parse(readFileSync(0, 'utf8')); await runDevWorktree({json.dumps(args)}, {{statePath: {json.dumps(state)}, acceptancePath: {json.dumps(acceptance_path)}, apiEnvironment}});"
            supervisor_environment = frontend_environment(environment)
            private_environment = application_environment(environment)
        supervisor = launch(
            ["node", "--input-type=module", "-e", expression],
            "supervisor",
            root,
            child_environment=supervisor_environment,
            private_environment=private_environment,
        )
        while supervisor.poll() is None and not stopping:
            if selected_inference is not None and not selected_inference.supervision_ready():
                raise WorktreeError("推理夹具 owner 意外退出；保留资源")
            if any(
                child.poll() is not None
                for child in children
                if child is not supervisor
            ):
                raise WorktreeError(
                    "本环境 worker 意外退出；停止 API/Web，避免显示虚假的就绪状态"
                )
            time.sleep(0.1)
        exit_code = (128 + stop_signal if canonical_e2e else 0) if stopping else supervisor.returncode
        return exit_code
    finally:
        original = sys.exception()
        cleanup_error = None
        try:
            if canonical_e2e and (original is not None or exit_code not in (None, 0)):
                retain_consumers("command_failed")
            if not stopping:
                signal_children(signal.SIGTERM)
            deadline = time.monotonic() + 12
            for child in children:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    if canonical_e2e:
                        raise WorktreeError("e2e 子进程未退出；保留运行记录")
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
            if canonical_e2e and original is None and exit_code == 0 and not stopping:
                record["supervisor_reaped"] = True
                atomic_json(record_path, record)
            if inference is not None and not record.get("unsettled_consumers"):
                inference.close()
        except BaseException as error:
            cleanup_error = error
        finally:
            for sig, handler in old_signals.items():
                signal.signal(sig, handler)
            marker = state_path(
                Path(resources["root"]), resources["mode"], "inference-owner.json"
            )
            if cleanup_error is None and not marker.exists() and not record.get("unsettled_consumers"):
                record_path.unlink(missing_ok=True)
        if cleanup_error is not None:
            if original is not None:
                raise BaseExceptionGroup(
                    "e2e 执行与 owner 清理均失败", [original, cleanup_error]
                ) from None
            if exit_code not in (None, 0):
                raise BaseExceptionGroup(
                    "e2e 命令失败且 owner 清理未完成",
                    [WorktreeError(f"e2e 命令退出码 {exit_code}"), cleanup_error],
                ) from None
            raise cleanup_error


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[1]
    if options.command in {"up", "init", "exec", "reset"}:
        subprocess.run(
            ["bash", str(root / "scripts/ensure-api-vendor.sh")], cwd=root, check=True
        )
    sys.path.insert(0, str(root / "apps/api"))
    from app.config import settings
    from worktree_resources import LocalResources

    if settings.environment != "development":
        raise WorktreeError(
            "此启动器仅用于 development，不接受 staging/production 配置"
        )
    create = options.command in {"up", "init", "exec"}
    try:
        identity = load_identity(root, create=create)
    except FileNotFoundError:
        if options.command == "doctor":
            print(
                json.dumps(
                    {
                        "initialized": False,
                        "root": str(root),
                        "hint": "运行 pnpm dev:worktree -- init",
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        raise WorktreeError("本工作树尚未初始化；先运行 init") from None
    resources = topology(root, identity, options.mode)
    backend = LocalResources(resources, settings, code_root=root)
    if options.command == "doctor":
        report = diagnose(backend)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["healthy"] else 1
    if options.command == "stop":
        stop_environment(backend)
        return 0
    with environment_lock(resources):
        if options.command == "recover-inference":
            from fixture_inference_services import FixtureInferenceServices

            reject_orphans(backend, allow_inference=True)
            inference = FixtureInferenceServices(backend, code_root=root)
            if options.finish_drain:
                inference.recover_drained()
                return 0
            inference.reattach()
            return run_processes(backend, options, fixture_inference=inference)
        reject_orphans(backend)
        if (
            options.command == "exec"
            and state_path(root, options.mode, "data", "filtering.json").exists()
        ):
            raise WorktreeError(
                "本环境保留了手动验收数据；先显式 reset，再运行 exec，避免自动化清理验收结果"
            )
        if options.command in {"destroy", "reset"}:
            backend.destroy(options.confirm)
            if options.command == "destroy":
                return 0
        if options.command in {"init", "reset"}:
            backend.prepare(skip_migrations=options.skip_migrations)
            print(
                json.dumps(
                    {"resources": resources, "ready": True},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        return run_processes(backend, options)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        print(f"[dev:worktree] {safe_error(error)}", file=sys.stderr)
        raise SystemExit(1) from None
