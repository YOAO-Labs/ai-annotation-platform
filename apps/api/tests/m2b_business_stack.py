"""Cold current-checkout processes on the module verifier's owned resources."""

from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from celery import Celery
import httpx
from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import (
    _local_s3_client,
    _shared_bootstrap_minio,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_identity_admission_live import Process
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]
API_LOGIN = "identity_embedded_api_consumer"
QUEUES = {"default", "media", "cleanup", "audit", "ml.cpu"}
_OBSERVATION = "m2b-physical-prediction "
_INFERENCE_CAPABILITIES = {
    "pipelines": "data",
    "backend_probe": "backend_probe_manager",
    "health_observation": "health_observation_manager",
    "registry_ingress": "registry_ingress_manager",
    "routing": "routing_manager",
    "execution": "model_execution_manager",
    "backend_control": "backend_control_manager",
}
_LOG_FAULTS = {
    b"InsufficientPrivilegeError": "42501",
    b"UndefinedFunctionError": "42883",
    b"UndefinedTableError": "42P01",
    b"UndefinedColumnError": "42703",
    b"InvalidPasswordError": "28P01",
    b"NotNullViolationError": "23502",
    b"UniqueViolationError": "23505",
    b"ConnectionRefusedError": "connection_refused",
    b"ModuleNotFoundError": "module_not_found",
    b"ValidationError": "validation_error",
}

_INFERENCE_READINESS = """
import json
import sys
import time

from yoao_inference.__main__ import main
from yoao_inference.pipeline_server import PipelineHttpServer

async def report(owner):
    checks = json.loads(sys.argv[1])
    started = time.monotonic()
    status, _ = await owner._dispatch("GET", "/health", {}, b"")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    results = {}
    for name in owner.required_capabilities:
        try:
            results[name] = await getattr(owner, checks[name]).readiness() is True
        except Exception:
            results[name] = False
    print(json.dumps([status, elapsed_ms, results]))

# Inspect a separate startup with the same owner configuration, without listening.
PipelineHttpServer.serve_forever = report
main()
"""

_AUTHOR = r"""
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import my_color_region_plugin
import uvicorn
import yoao_inference_plugin
import yoao_protocol_v2
from my_color_region_plugin import Provider
from yoao_inference_plugin import DetectionConfig, create_detection_app

assert sys.prefix != sys.base_prefix and find_spec('app') is None
for module in (my_color_region_plugin, yoao_inference_plugin, yoao_protocol_v2):
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in path.parts
host, port, storage_origin, token_path, marker = sys.argv[1:]
config = DetectionConfig(
    token=Path(token_path).read_text(), device='cpu', catalog={'colors': {}},
    default_model='colors', series='color-region', allowed_image_hosts=(host,),
)
app = create_detection_app(Provider(), config)

@app.middleware('http')
async def observe_prediction(request, call_next):
    if request.method != 'POST' or request.url.path != '/predict':
        return await call_next(request)
    body = await request.json()
    response = await call_next(request)
    if response.status_code == 200:
        tasks = body['tasks'] if 'tasks' in body else [body['task']]
        for task in tasks:
            url = urlsplit(task['file_path'])
            params = parse_qs(url.query)
            print(marker + json.dumps({
                'pid': os.getpid(),
                'task_id': str(task['id']),
                'object_store': f'{url.scheme}://{url.netloc}' == storage_origin,
                'signed_and_versioned': all(params.get(key) for key in (
                    'X-Amz-Signature', 'X-Amz-Expires', 'versionId',
                )),
                'bucket': url.path.split('/')[1] if url.path.startswith('/') else '',
            }), flush=True)
    return response

uvicorn.run(app, host=host, port=int(port), access_log=False, timeout_graceful_shutdown=3)
"""


def _port(host: str = "127.0.0.1") -> int:
    with socket.socket() as listener:
        listener.bind((host, 0))
        return listener.getsockname()[1]


def _role_dsn(test_engine, profile: str, role: str, *, native: bool = False) -> str:
    url = make_url(_role_url(f"AAP_MODULE_DATA_{profile}_URL", role))
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return url.set(
        drivername="postgresql" if native else "postgresql+asyncpg"
    ).render_as_string(hide_password=False)


def _spawn(processes, directory, name, command, environment, *, origin="") -> Process:
    log = processes.enter_context(
        open(directory / f"{name}.log", "xb", opener=_private)
    )
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    service = Process(process, log, origin)
    processes.callback(service.stop)
    return service


def _private(path, flags):
    return os.open(path, flags, 0o600)


def _process_status(service) -> str:
    try:
        with open(service.log.name, "rb") as log:
            log.seek(-min(os.fstat(log.fileno()).st_size, 32768), os.SEEK_END)
            tail = log.read(32768)
    except OSError:
        return f"exit={service.process.poll()}, private log unavailable"
    faults = ",".join(code for marker, code in _LOG_FAULTS.items() if marker in tail)
    return f"exit={service.process.poll()}, known_faults={faults or 'none'}"


def _inference_readiness(directory, environment) -> str:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _INFERENCE_READINESS,
                json.dumps(_INFERENCE_CAPABILITIES),
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "private readiness probe unavailable"
    if result.returncode != 0:
        return f"private readiness probe exit={result.returncode}"
    try:
        status, elapsed_ms, readiness = json.loads(result.stdout)
    except (TypeError, UnicodeError, ValueError):
        return "private readiness probe did not return a report"
    if (
        type(status) is not int
        or status not in (200, 503)
        or type(elapsed_ms) is not int
        or not 0 <= elapsed_ms <= 30000
        or not isinstance(readiness, dict)
        or readiness.keys() != _INFERENCE_CAPABILITIES.keys()
        or not all(type(ready) is bool for ready in readiness.values())
    ):
        return "private readiness probe did not report every required capability"
    failed = ",".join(name for name, ready in readiness.items() if not ready)
    return (
        f"private readiness HTTP={status}, elapsed_ms={elapsed_ms}, "
        f"not_ready={failed or 'none'}"
    )


def _wait_http(
    client, service, component: str, health: str, *, timeout=2, diagnose=None
) -> None:
    deadline = time.monotonic() + 45
    last_result = "no response"
    while time.monotonic() < deadline and service.process.poll() is None:
        try:
            status = client.get(service.origin + health, timeout=timeout).status_code
            last_result = f"HTTP {status}"
            if status == 200:
                return
        except httpx.TimeoutException:
            last_result = "request timeout"
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    details = f"; {diagnose()}" if diagnose is not None else ""
    raise AssertionError(
        f"{component} {health} not ready: {last_result}, "
        f"{_process_status(service)}{details}"
    )


def _wait_worker(worker, redis_url, name: str) -> tuple[str, ...]:
    from worktree_runtime import worker_queues_match

    configured = ",".join(sorted(QUEUES))
    with Celery(broker=redis_url) as probe:
        inspector = probe.control.inspect(destination=[name], timeout=1)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert worker.process.poll() is None, (
                f"Celery exited: {_process_status(worker)}"
            )
            registered = (inspector.registered() or {}).get(name, [])
            if "app.workers.tasks.batch_predict" in registered:
                queues = (inspector.active_queues() or {}).get(name)
                if queues is not None:
                    assert worker_queues_match(name, configured, queues), (
                        "Celery subscribed to unexpected queues"
                    )
                    stats = (inspector.stats() or {}).get(name)
                    if stats is None:
                        continue
                    assert stats.get("pid") == worker.process.pid
                    return tuple(sorted(queue["name"] for queue in queues))
        raise AssertionError(
            f"Celery did not register batch_predict: {_process_status(worker)}"
        )


def _inference_environment(test_engine, common, port):
    owner = {
        **common,
        "INFERENCE_PORT": str(port),
        "INFERENCE_REQUIRED_CAPABILITIES": ",".join(_INFERENCE_CAPABILITIES),
        "GPU_ARBITER_MODE": "off",
    }
    caller = {}
    origin = f"http://127.0.0.1:{port}"
    for prefix, caller_token, owner_token, role in (
        (
            "INFERENCE_PIPELINES",
            "INFERENCE_PIPELINES_TOKEN",
            "INFERENCE_CONTROL_TOKEN",
            "yoao_inference_pipelines_client",
        ),
        (
            "INFERENCE_BACKEND_PROBE",
            "INFERENCE_BACKEND_PROBE_TOKEN",
            "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN",
            "yoao_inference_backend_probe_client",
        ),
        (
            "INFERENCE_HEALTH_OBSERVATION",
            "INFERENCE_HEALTH_OBSERVATION_TOKEN",
            "INFERENCE_HEALTH_OBSERVATION_TOKEN",
            "yoao_inference_health_observation_client",
        ),
        (
            "INFERENCE_REGISTRY_INGRESS",
            "INFERENCE_REGISTRY_INGRESS_TOKEN",
            "INFERENCE_REGISTRY_INGRESS_TOKEN",
            "yoao_inference_registry_ingress_client",
        ),
        (
            "INFERENCE_ROUTING",
            "INFERENCE_ROUTING_TOKEN",
            "INFERENCE_ROUTING_TOKEN",
            "yoao_inference_routing_client",
        ),
        (
            "INFERENCE_MODEL_EXECUTION",
            "INFERENCE_MODEL_EXECUTION_TOKEN",
            "INFERENCE_MODEL_EXECUTION_TOKEN",
            None,
        ),
        (
            "INFERENCE_BACKEND_CONTROL",
            "INFERENCE_BACKEND_CONTROL_TOKEN",
            "INFERENCE_BACKEND_CONTROL_TOKEN",
            None,
        ),
    ):
        token = secrets.token_urlsafe(32)
        caller[f"{prefix}_URL"] = origin
        caller[caller_token] = owner[owner_token] = token
        if role is not None:
            owner[f"{prefix}_DATABASE_URL"] = _role_dsn(
                test_engine, prefix, role, native=True
            )
    for purpose, role_suffix in (
        ("SETUP", "setup"),
        ("HEALTH_OBSERVATION", "health"),
        ("CATALOG_FALLBACK", "catalog"),
        ("STATS", "stats"),
        ("BACKEND_READ", "read"),
        ("LIFECYCLE", "lifecycle"),
        ("EXECUTION", "execution"),
    ):
        owner[f"INFERENCE_BACKEND_ENDPOINT_{purpose}_DATABASE_URL"] = _role_dsn(
            test_engine,
            f"INFERENCE_ENDPOINT_{purpose}",
            f"yoao_inference_endpoint_{role_suffix}_client",
            native=True,
        )
    for setting, profile, role in (
        ("HEALTH_DATA", "HEALTH_DATA", "health_data"),
        ("BACKEND_STATS", "BACKEND_STATS", "backend_stats"),
        ("GPU", "GPU", "gpu_authority"),
        (
            "GPU_ENDPOINT_HEALTH_OBSERVATION",
            "GPU_HEALTH_ENDPOINT",
            "gpu_health_endpoint",
        ),
        ("GPU_ENDPOINT_GPU_CONTROL", "GPU_CONTROL_ENDPOINT", "gpu_control_endpoint"),
    ):
        owner[f"INFERENCE_{setting}_DATABASE_URL"] = _role_dsn(
            test_engine,
            f"INFERENCE_{profile}",
            f"yoao_inference_{role}_client",
            native=True,
        )
    return origin, owner, caller


@dataclass
class BusinessStack:
    api: Process
    worker: Process
    inference: Process
    author: Process
    author_token: str = field(repr=False)
    database_url: str = field(repr=False)
    storage_origin: str
    inputs_bucket: str
    worker_queues: tuple[str, ...]

    def assert_alive(self) -> None:
        for name, service in self._processes().items():
            assert service.process.poll() is None, f"{name}: {_process_status(service)}"

    def model_calls(self) -> list[dict]:
        return [
            json.loads(line[len(_OBSERVATION) :])
            for line in Path(self.author.log.name).read_text().splitlines()
            if line.startswith(_OBSERVATION)
        ]

    def _processes(self) -> dict[str, Process]:
        return {
            "api": self.api,
            "celery": self.worker,
            "inference": self.inference,
            "cpu_plugin": self.author,
        }

    def process_ids(self) -> dict[str, int]:
        pids = {
            name: service.process.pid for name, service in self._processes().items()
        }
        assert len({os.getpid(), *pids.values()}) == len(pids) + 1
        return pids

    def diagnostics(self) -> str:
        return "; ".join(
            f"{name}: {_process_status(service)}"
            for name, service in self._processes().items()
        )


@contextmanager
def business_stack(test_engine, directory):
    dotenv = ROOT / ".env"
    assert not dotenv.exists() and not dotenv.is_symlink(), (
        "run in a clean checkout without .env"
    )
    assert os.environ.get("YOAO_WORKTREE_MODE") == "test"
    _verify_postgres(test_engine.url)
    redis_url = _verify_redis().render_as_string(hide_password=False)
    minio_id = os.environ["AAP_MODULE_DATA_MINIO_CONTAINER_ID"]
    assert _shared_bootstrap_minio(settings, container_id=minio_id) is not None, (
        "set AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER to an independent verifier MinIO"
    )
    owner = _verified_resource_manifest(test_engine, test_engine.url)
    s3_key, s3_secret = os.environ["MINIO_ACCESS_KEY"], os.environ["MINIO_SECRET_KEY"]
    with closing(_local_s3_client(settings, s3_key, s3_secret)) as storage:
        for bucket in owner["buckets"].values():
            _verify_bucket_owner(storage, bucket, owner["owner"])
    inputs_bucket = owner["buckets"]["MINIO_INPUTS_BUCKET"]
    scheme = "https" if settings.minio_use_ssl else "http"
    storage_origin = f"{scheme}://{settings.minio_endpoint}"
    author_host = urlsplit(storage_origin).hostname
    assert author_host is not None
    author_python = Path(os.environ["AAP_M2B_CPU_PLUGIN_PYTHON"])
    assert author_python.is_absolute() and os.access(author_python, os.X_OK)
    assert not author_python.is_relative_to(ROOT), "use an independent author venv"

    common = {
        "PATH": os.defpath,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "TMPDIR": str(directory),
    }
    db_url = _role_dsn(test_engine, "IDENTITY_EMBEDDED_API_CONSUMER", API_LOGIN)
    with (
        ExitStack() as processes,
        httpx.Client(trust_env=False, timeout=2) as readiness,
    ):
        author_token = secrets.token_urlsafe(32)
        token_path = directory / "author-token"
        with open(token_path, "x", opener=_private) as token_file:
            token_file.write(author_token)
        processes.callback(token_path.unlink)
        author_port = _port(author_host)
        author = _spawn(
            processes,
            directory,
            "color-plugin",
            [
                str(author_python),
                "-I",
                "-c",
                _AUTHOR,
                author_host,
                str(author_port),
                storage_origin,
                str(token_path),
                _OBSERVATION,
            ],
            common,
            origin=f"http://{author_host}:{author_port}",
        )
        _wait_http(readiness, author, "CPU plugin", "/health")
        assert readiness.get(author.origin + "/setup").status_code == 403

        inference_origin, owner_env, inference_caller_env = _inference_environment(
            test_engine, common, _port()
        )
        inference = _spawn(
            processes,
            directory,
            "inference",
            [sys.executable, "-m", "yoao_inference"],
            owner_env,
            origin=inference_origin,
        )
        _wait_http(
            readiness,
            inference,
            "Inference owner",
            "/health",
            timeout=8,
            diagnose=lambda: _inference_readiness(directory, owner_env),
        )

        app_env = {
            **common,
            **owner["buckets"],
            **inference_caller_env,
            "PYTHONPATH": str(ROOT / "apps/api"),
            "ENVIRONMENT": "development",
            "SECRET_KEY": secrets.token_urlsafe(32),
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "DATABASE_URL": db_url,
            "REDIS_URL": redis_url,
            "CELERY_BROKER_URL": redis_url,
            "MINIO_ENDPOINT": settings.minio_endpoint,
            "MINIO_PUBLIC_URL": storage_origin,
            "MINIO_USE_SSL": str(settings.minio_use_ssl),
            "MINIO_ACCESS_KEY": s3_key,
            "MINIO_SECRET_KEY": s3_secret,
            "AWS_EC2_METADATA_DISABLED": "true",
            "DUCKDB_PATH": str(directory / "analytics.duckdb"),
            "ML_MANAGED_BACKEND_URLS": "{}",
            "ML_BACKEND_STORAGE_HOST": settings.minio_endpoint,
            "JOBS_PUBLICATION_DATABASE_URL": _role_dsn(
                test_engine, "JOBS_PUBLICATION", "yoao_jobs_publication_client"
            ),
        }
        api_port = _port()
        api = _spawn(
            processes,
            directory,
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--no-access-log",
                "--timeout-graceful-shutdown",
                "3",
            ],
            app_env,
            origin=f"http://127.0.0.1:{api_port}",
        )
        for component in ("db", "redis", "minio"):
            _wait_http(readiness, api, "API", f"/health/{component}")
        worker_name = f"m2b-business-{uuid4().hex}@local"
        worker = _spawn(
            processes,
            directory,
            "celery",
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--loglevel=INFO",
                f"--hostname={worker_name}",
                "--queues=" + ",".join(sorted(QUEUES)),
            ],
            {
                **app_env,
                "ASSETS_CELERY_VIDEO_DATABASE_URL": _role_dsn(
                    test_engine, "ASSET_MEDIA_WORKER", "yoao_asset_media_worker_client"
                ),
                "ASSETS_CELERY_IMAGE_DATABASE_URL": _role_dsn(
                    test_engine, "ASSET_IMAGE_WORKER", "yoao_asset_image_worker_client"
                ),
                "PREANNOTATION_RESULTS_DATABASE_URL": _role_dsn(
                    test_engine,
                    "PREANNOTATION_RESULT",
                    "yoao_preannotation_result_client",
                ),
            },
        )
        worker_queues = _wait_worker(worker, redis_url, worker_name)
        stack = BusinessStack(
            api,
            worker,
            inference,
            author,
            author_token,
            db_url,
            storage_origin,
            inputs_bucket,
            worker_queues,
        )
        try:
            yield stack
        except Exception as error:
            error.add_note(stack.diagnostics())
            raise
