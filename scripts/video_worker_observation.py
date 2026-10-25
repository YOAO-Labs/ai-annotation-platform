"""Read exact local worker identity, credential selectors and process exit facts.

Run from the repository root with ``python -m scripts.video_worker_observation``.
This observer never stops containers or grants Jobs retirement authorization.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import subprocess
from typing import Any

from sqlalchemy.engine import make_url

from scripts.video_worker_fence import (
    LocalFenceRefused,
    WorkerDatabaseCredential,
    read_private_json,
)


WORKER_DATABASE_SOURCES = {
    "DATABASE_URL": "identity_api_consumer",
    "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "preannotation_result_client",
    "INFERENCE_ROUTING_DATABASE_URL": "inference_routing_client",
    "LABELING_VIDEO_WRITER_DATABASE_URL": "labeling_video_writer_client",
}
_ROLE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_WORKER_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{20}\Z")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{7,64}\Z")
_QUEUE_NAME = re.compile(r"legacy_video_fence_[0-9a-f]{20}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class WorkerCredentialInventory:
    identity: dict[str, Any]
    credentials: tuple[WorkerDatabaseCredential, ...] = field(repr=False)
    write_selectors: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "database_credentials": [item.evidence() for item in self.credentials],
            "write_selectors": dict(self.write_selectors),
        }


def inventory_worker_database_credentials(
    environment: Mapping[str, str],
    *,
    database: str,
    read_secret_file: Callable[[str], str],
    container_host: str = "worker-postgres",
    container_port: int = 5432,
) -> tuple[WorkerDatabaseCredential, ...]:
    """Parse observed worker selectors, refusing unknown nonempty DB bindings."""
    observed: dict[str, WorkerDatabaseCredential] = {}
    for source, raw in sorted(environment.items()):
        if "DATABASE_URL" not in source:
            continue
        if not isinstance(raw, str):
            raise LocalFenceRefused("worker_database_environment_invalid")
        if not raw:
            continue
        if source.endswith("_FILE"):
            if source != "PREANNOTATION_RESULTS_DATABASE_URL_FILE":
                raise LocalFenceRefused("unrecognized_worker_database_secret_file")
            dsn_text = read_secret_file(raw)
            secret_file = True
        else:
            if source not in WORKER_DATABASE_SOURCES:
                raise LocalFenceRefused("unrecognized_worker_database_selector")
            dsn_text = raw
            secret_file = False
        expected_role = WORKER_DATABASE_SOURCES.get(source)
        if expected_role is None:
            raise LocalFenceRefused("unrecognized_worker_database_selector")
        try:
            url = make_url(dsn_text)
        except Exception:
            raise LocalFenceRefused("worker_database_identity_invalid") from None
        if (
            url.drivername not in {"postgresql", "postgresql+asyncpg"}
            or url.username != expected_role
            or not url.password
            or url.host != container_host
            or url.port != container_port
            or url.database != database
            or url.query
            or not _ROLE_NAME.fullmatch(url.username or "")
        ):
            raise LocalFenceRefused("worker_database_identity_mismatch")
        if source in observed:
            raise LocalFenceRefused("duplicate_worker_database_selector")
        observed[source] = WorkerDatabaseCredential(
            source_variable=source,
            role=expected_role,
            database=database,
            container_host=container_host,
            container_port=container_port,
            secret_file=secret_file,
            _url=url,
        )
    if set(observed) != set(WORKER_DATABASE_SOURCES):
        raise LocalFenceRefused("incomplete_worker_database_inventory")
    roles = [item.role for item in observed.values()]
    if len(roles) != len(set(roles)):
        raise LocalFenceRefused("ambiguous_worker_database_inventory")
    return tuple(observed[source] for source in sorted(observed))


def inspect_worker_write_selectors(
    environment: Mapping[str, str],
    *,
    expected_s3_access_key: str,
    expected_media_token: str,
) -> dict[str, Any]:
    """Bind non-DB selectors to the explicitly supplied local peer identities."""
    if not expected_s3_access_key or not expected_media_token:
        raise LocalFenceRefused("worker_write_selector_identity_missing")
    sensitive = {
        key: value
        for key, value in environment.items()
        if re.search(r"(?i)(?:TOKEN|KEY|SECRET|PASSWORD)", key)
        and isinstance(value, str)
        and value
        and not (key == "GPG_KEY" and re.fullmatch(r"[0-9A-F]{40}", value))
    }
    if set(sensitive) != {
        "SECRET_KEY",
        "ASSETS_MEDIA_TOKEN",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    }:
        raise LocalFenceRefused("unrecognized_worker_write_selector")
    if (
        sensitive.get("ASSETS_MEDIA_TOKEN") != expected_media_token
        or sensitive.get("MINIO_ACCESS_KEY") != expected_s3_access_key
        or not sensitive.get("MINIO_SECRET_KEY")
        or not sensitive.get("SECRET_KEY")
        or environment.get("INFERENCE_ROUTING_TOKEN")
        or environment.get("LABELING_VIDEO_WRITER_CONTROL_TOKEN")
        or environment.get("INFERENCE_ROUTING_TOKEN_FILE")
        or environment.get("LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE")
    ):
        raise LocalFenceRefused("worker_write_selector_mismatch")
    return {
        "database_roles": sorted(WORKER_DATABASE_SOURCES.values()),
        "media_control_token": "present_and_bound_to_owned_media_peer",
        "s3_principal_id": expected_s3_access_key,
        "process_secret": "present_local_worker_secret",
        "direct_routing_token": "absent",
        "direct_writer_control_token": "absent",
    }


def validate_owned_worker_container(
    record: Mapping[str, Any],
    *,
    container_id: str,
    container_name: str,
    acceptance_id: str,
    image_id: str,
    source_revision: str,
    queue: str,
    expected_running: bool = True,
) -> dict[str, Any]:
    """Verify one exact running worker, image, restart policy and private queue."""
    config = record.get("Config") or {}
    labels = config.get("Labels") or {}
    restart_policy = (record.get("HostConfig") or {}).get("RestartPolicy") or {}
    command = config.get("Cmd") or []
    queue_flags = [item for item in command if item.startswith("--queues=")]
    if not (
        _WORKER_ACCEPTANCE_ID.fullmatch(acceptance_id)
        and record.get("Id") == container_id
        and record.get("Name") == f"/{container_name}"
        and labels.get("org.aap.acceptance") == "workspace-worker-assembly"
        and labels.get("org.aap.acceptance-id") == acceptance_id
        and labels.get("org.aap.source-revision") == source_revision
        and _SOURCE_REVISION.fullmatch(source_revision)
        and record.get("Image") == image_id
        and (record.get("State") or {}).get("Running") is expected_running
        and restart_policy.get("Name", "") in {"", "no"}
        and (record.get("RestartCount") or 0) == 0
        and _QUEUE_NAME.fullmatch(queue)
        and queue_flags == [f"--queues={queue}"]
    ):
        raise LocalFenceRefused("worker_resource_ownership_mismatch")
    return {
        "container_id": container_id,
        "container_name": container_name,
        "acceptance_id": acceptance_id,
        "image_id": image_id,
        "source_revision": source_revision,
        "queue": queue,
        "restart_policy": restart_policy.get("Name") or "no",
        "restart_count": record.get("RestartCount") or 0,
    }


@dataclass(frozen=True)
class LocalWorkerTarget:
    container_id: str
    container_name: str
    acceptance_id: str
    image_id: str
    image_acceptance_id: str
    source_revision: str
    network_id: str
    network_name: str
    database: str
    queue: str
    worker_started_at: str
    result_dsn_file: str
    postgres_container_id: str
    redis_container_id: str


def _docker(*args: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=15
        )
        return result.stdout.strip()
    except Exception:
        raise LocalFenceRefused("worker_docker_observation_unavailable") from None


def _inspect(kind: str, identity: str) -> dict:
    try:
        values = json.loads(_docker("inspect", "--type", kind, identity))
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError
        return values[0]
    except LocalFenceRefused:
        raise
    except Exception:
        raise LocalFenceRefused("worker_docker_observation_unavailable") from None


def _worker_record(target: LocalWorkerTarget, *, stopped: bool = False) -> dict:
    if (
        not all(
            _CONTAINER_ID.fullmatch(item)
            for item in (
                target.container_id,
                target.network_id,
                target.postgres_container_id,
                target.redis_container_id,
            )
        )
        or target.container_name != f"aap-workspace-worker-{target.acceptance_id}"
        or target.network_name != target.container_name
        or not target.database.endswith("_test")
        or not target.worker_started_at
    ):
        raise LocalFenceRefused("worker_resource_ownership_mismatch")
    record = _inspect("container", target.container_id)
    state = record.get("State") or {}
    validate_owned_worker_container(
        record,
        container_id=target.container_id,
        container_name=target.container_name,
        acceptance_id=target.acceptance_id,
        image_id=target.image_id,
        source_revision=target.source_revision,
        queue=target.queue,
        expected_running=not stopped,
    )
    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    if (
        state.get("StartedAt") != target.worker_started_at
        or state.get("Paused") is True
        or state.get("Dead") is True
        or labels.get("com.docker.compose.project") is not None
        or config.get("User") != "10003:10003"
        or host.get("ReadonlyRootfs") is not True
        or "ALL" not in (host.get("CapDrop") or [])
        or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
        or host.get("Memory") != 512 * 1024 * 1024
        or host.get("NanoCpus") != 1_000_000_000
        or host.get("PidsLimit") != 64
    ):
        raise LocalFenceRefused("worker_runtime_identity_mismatch")
    if stopped:
        if (
            state.get("Running") is not False
            or state.get("Status") != "exited"
            or state.get("ExitCode") not in {0, 137}
            or not state.get("FinishedAt")
        ):
            raise LocalFenceRefused("worker_stop_boundary_unverified")
    elif state.get("Running") is not True:
        raise LocalFenceRefused("worker_not_running")
    image = _inspect("image", target.image_id)
    image_labels = (image.get("Config") or {}).get("Labels") or {}
    if (
        image.get("Id") != target.image_id
        or image_labels.get("org.aap.acceptance") != "workspace-module-api"
        or image_labels.get("org.aap.acceptance-id") != target.image_acceptance_id
        or image_labels.get("org.aap.source-revision") != target.source_revision
    ):
        raise LocalFenceRefused("worker_image_identity_mismatch")
    network = _inspect("network", target.network_id)
    network_labels = network.get("Labels") or {}
    peers = {target.postgres_container_id, target.redis_container_id}
    attached = set((network.get("Containers") or {}).keys())
    networks = (record.get("NetworkSettings") or {}).get("Networks") or {}
    if (
        network.get("Id") != target.network_id
        or network.get("Name") != target.network_name
        or network_labels.get("org.aap.acceptance") != "workspace-worker-assembly"
        or network_labels.get("org.aap.acceptance-id") != target.acceptance_id
        or set(networks) != {target.network_name}
        or networks[target.network_name].get("NetworkID") != target.network_id
        or (
            attached not in (peers, peers | {target.container_id})
            if stopped
            else attached != peers | {target.container_id}
        )
    ):
        raise LocalFenceRefused("worker_private_network_identity_mismatch")
    mounts = [
        mount for mount in (record.get("Mounts") or []) if mount.get("Type") == "bind"
    ]
    if (
        len(mounts) != 1
        or mounts[0].get("Destination") != "/run/secrets/preannotation_results_dsn"
        or mounts[0].get("Source") != target.result_dsn_file
        or mounts[0].get("RW") is not False
    ):
        raise LocalFenceRefused("worker_secret_mount_ownership_mismatch")
    return record


def observe_worker_credentials(
    target: LocalWorkerTarget,
    *,
    expected_s3_access_key: str,
    expected_media_token: str,
) -> WorkerCredentialInventory:
    """Read actual Docker env and the exact mounted secret; never host settings."""
    record = _worker_record(target)
    environment = {}
    for entry in (record.get("Config") or {}).get("Env") or []:
        key, value = entry.split("=", 1)
        if key in environment:
            raise LocalFenceRefused("duplicate_worker_environment_selector")
        environment[key] = value

    def read_secret(path: str) -> str:
        if path != "/run/secrets/preannotation_results_dsn":
            raise LocalFenceRefused("worker_secret_mount_ownership_mismatch")
        return _docker(
            "exec",
            target.container_id,
            "/opt/venv/bin/python",
            "-I",
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
            path,
        )

    credentials = inventory_worker_database_credentials(
        environment,
        database=target.database,
        read_secret_file=read_secret,
    )
    selectors = inspect_worker_write_selectors(
        environment,
        expected_s3_access_key=expected_s3_access_key,
        expected_media_token=expected_media_token,
    )
    _worker_record(target)
    return WorkerCredentialInventory(
        identity={
            "container_id": target.container_id,
            "container_name": target.container_name,
            "acceptance_id": target.acceptance_id,
            "image_id": target.image_id,
            "image_acceptance_id": target.image_acceptance_id,
            "source_revision": target.source_revision,
            "network_id": target.network_id,
            "network_name": target.network_name,
            "database": target.database,
            "queue": target.queue,
            "worker_started_at": target.worker_started_at,
            "restart_policy": "no",
            "restart_count": 0,
        },
        credentials=credentials,
        write_selectors=selectors,
    )


def observe_worker_exit(target: LocalWorkerTarget) -> dict[str, Any]:
    """Observe a stopped exact container before its owner removes it."""
    record = _worker_record(target, stopped=True)
    return {
        "schema": "aap-local-worker-exit-facts/v1",
        "container_id": target.container_id,
        "image_id": target.image_id,
        "network_id": target.network_id,
        "started_at": record["State"]["StartedAt"],
        "finished_at": record["State"]["FinishedAt"],
        "exit_code": record["State"]["ExitCode"],
        "restart_count": record.get("RestartCount") or 0,
        "restart_policy": "no",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "external_restart_controllers_fenced": False,
        "jobs_legacy_retirement_proof_created": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--state", choices=("running", "stopped"), default="running")
    args = parser.parse_args()
    try:
        value = read_private_json(args.input_file)
        if (
            set(value)
            != {"schema", "worker", "expected_s3_access_key", "expected_media_token"}
            or value["schema"] != "aap-local-worker-observation-input/v1"
        ):
            raise LocalFenceRefused("invalid_operator_input")
        target = LocalWorkerTarget(**value["worker"])
        if args.state == "stopped":
            output = observe_worker_exit(target)
        else:
            inventory = observe_worker_credentials(
                target,
                expected_s3_access_key=value["expected_s3_access_key"],
                expected_media_token=value["expected_media_token"],
            )
            output = {
                "schema": "aap-local-worker-credential-facts/v1",
                **inventory.evidence(),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "historical_credential_inventory_complete": False,
                "jobs_legacy_retirement_proof_created": False,
            }
        code = 0
    except LocalFenceRefused as error:
        output, code = {"reason": str(error)}, 2
    except Exception:
        output, code = {"reason": "invalid_operator_input"}, 2
    print(json.dumps(output, sort_keys=True, allow_nan=False))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
