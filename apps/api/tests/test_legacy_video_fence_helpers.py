"""Pure fail-closed checks for the local legacy-video fence fixture."""

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import asyncpg
from botocore.exceptions import ClientError
import pytest
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from scripts.video_storage_fence import S3_PROBE_BODY, ScopedStorageFenceProbe
from scripts.video_worker_fence import (
    LocalFenceRefused,
    prove_stale_worker_dsns_denied,
    validate_owned_postgres_container,
)
from scripts.video_worker_observation import (
    inventory_worker_database_credentials,
    inspect_worker_write_selectors,
    validate_owned_worker_container,
)
from tests.legacy_video_fence_helpers import (
    build_local_fence_evidence,
    owned_worker_identity,
    run_fence_after_preflight,
    stop_fixture_workers,
    stop_owned_worker,
    validate_sanitized_local_fence_evidence,
    worker_credential_evidence,
)


_DATABASE = "aap_module_data_123456_test"
_S3_PROBE = {
    "operation": "PutObject",
    "key": "projects/01234567-89ab-cdef-0123-456789abcdef/guide/legacy-video-fence-"
    + "1" * 32
    + ".txt",
    "payload_sha256": sha256(S3_PROBE_BODY).hexdigest(),
    "succeeded_before_revoke": True,
    "version_id": None,
}
_WORKER_DSN = URL.create(
    "postgresql+asyncpg",
    username="identity_api_consumer",
    password="worker-secret",
    host="worker-postgres",
    port=5432,
    database=_DATABASE,
).render_as_string(hide_password=False)
_RESULT_DSN = URL.create(
    "postgresql",
    username="preannotation_result_client",
    password="result-secret",
    host="worker-postgres",
    port=5432,
    database=_DATABASE,
).render_as_string(hide_password=False)
_ROUTING_DSN = URL.create(
    "postgresql",
    username="inference_routing_client",
    password="routing-secret",
    host="worker-postgres",
    port=5432,
    database=_DATABASE,
).render_as_string(hide_password=False)
_WRITER_DSN = URL.create(
    "postgresql",
    username="labeling_video_writer_client",
    password="writer-secret",
    host="worker-postgres",
    port=5432,
    database=_DATABASE,
).render_as_string(hide_password=False)


@pytest.mark.parametrize("denial_code", ["AccessDenied", "NoSuchBucket", None])
def test_s3_revocation_repeats_the_previously_authorized_write(denial_code):
    class Client:
        revoked = False

        def put_object(self, **request):
            if self.revoked and denial_code is not None:
                raise ClientError({"Error": {"Code": denial_code}}, "PutObject")
            return {}

        def get_object(self, **request):
            return {"Body": BytesIO(S3_PROBE_BODY)}

    client = Client()
    probe = ScopedStorageFenceProbe(client, "owned-bucket", _S3_PROBE["key"])
    with pytest.raises(LocalFenceRefused, match="still_works"):
        probe.observe_denied()
    client.revoked = True
    if denial_code == "AccessDenied":
        assert probe.observe_denied()["denial_code"] == denial_code
    else:
        with pytest.raises(LocalFenceRefused, match="inconclusive|still_works"):
            probe.observe_denied()


def test_worker_cleanup_covers_partial_start_and_reports_failure():
    class Worker:
        container_id = None
        network_id = "owned-network"
        stopped = False

        def stop(self):
            self.stopped = True

    class FailedWorker(Worker):
        def stop(self):
            raise RuntimeError("synthetic failure")

    partial = Worker()
    errors = stop_fixture_workers(FailedWorker(), None, partial)
    assert partial.stopped
    assert len(errors) == 1
    assert str(errors[0]) == "fixture_worker_cleanup_failed"


@pytest.mark.parametrize(
    "failure",
    [
        asyncpg.InvalidPasswordError("synthetic denial"),
        DBAPIError(
            "synthetic probe", {}, asyncpg.InvalidAuthorizationSpecificationError()
        ),
        TimeoutError("synthetic timeout"),
    ],
)
async def test_stale_login_proof_accepts_only_authentication_denials(
    monkeypatch, failure
):
    class Engine:
        @asynccontextmanager
        async def connect(self):
            raise failure
            yield

        async def dispose(self):
            pass

    monkeypatch.setattr(
        "scripts.video_worker_fence.create_async_engine", lambda *a, **kw: Engine()
    )
    credentials = inventory_worker_database_credentials(
        _environment(), database=_DATABASE, read_secret_file=lambda path: _RESULT_DSN
    )
    if isinstance(failure, TimeoutError):
        with pytest.raises(LocalFenceRefused, match="inconclusive"):
            await prove_stale_worker_dsns_denied(
                credentials, host="127.0.0.1", port=55432, database=_DATABASE
            )
    else:
        denied = await prove_stale_worker_dsns_denied(
            credentials, host="127.0.0.1", port=55432, database=_DATABASE
        )
        assert {item["role"] for item in denied} == {
            "identity_api_consumer",
            "preannotation_result_client",
            "inference_routing_client",
            "labeling_video_writer_client",
        }


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": _WORKER_DSN,
        "PREANNOTATION_RESULTS_DATABASE_URL_FILE": (
            "/run/secrets/preannotation_results_dsn"
        ),
        "INFERENCE_ROUTING_DATABASE_URL": _ROUTING_DSN,
        "LABELING_VIDEO_WRITER_DATABASE_URL": _WRITER_DSN,
        "MIGRATION_DATABASE_URL": "",
        "INFERENCE_ROUTING_DATABASE_URL_FILE": "",
    }


def _postgres_record() -> dict:
    return {
        "Id": "a" * 64,
        "Name": "/aap-module-data-123456abcdef",
        "Config": {
            "Image": "postgres:16-alpine",
            "Labels": {
                "org.aap.acceptance": "module-data-boundary",
                "org.aap.acceptance-id": "b" * 24,
            },
        },
        "State": {"Running": True},
        "NetworkSettings": {
            "Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
        },
    }


def _worker_record() -> dict:
    return {
        "Id": "c" * 64,
        "Name": "/aap-workspace-worker-0123456789abcdef0123",
        "Image": "sha256:" + "d" * 64,
        "Config": {
            "Labels": {
                "org.aap.acceptance": "workspace-worker-assembly",
                "org.aap.acceptance-id": "0123456789abcdef0123",
                "org.aap.source-revision": "e" * 40,
            },
            "Cmd": ["worker", "--queues=legacy_video_fence_0123456789abcdef0123"],
        },
        "HostConfig": {"RestartPolicy": {"Name": ""}},
        "State": {"Running": True, "StartedAt": "2026-10-23T00:00:00Z"},
        "RestartCount": 0,
    }


def _preflight() -> dict:
    credentials = inventory_worker_database_credentials(
        _environment(),
        database=_DATABASE,
        read_secret_file=lambda _path: _RESULT_DSN,
    )
    return {
        "schema": "aap-local-video-fence-preflight/v1",
        "scope": {
            "job_id": "01234567-89ab-cdef-0123-456789abcdef",
            "owner": "tracker:01234567-89ab-cdef-0123-456789abcdef",
            "operation": "tracker",
            "registry_id": "f" * 36,
            "scope_id": "11234567-89ab-cdef-0123-456789abcdef",
            "closed": False,
            "state": "active",
            "attempt_count": 0,
            "permit_count": 0,
            "no_live_admission": True,
            "job_status": "running",
            "job_started_at": "2026-10-23T00:00:00Z",
            "input_referenced": True,
            "deletion_guard_still_409": True,
            "retirement_phase": None,
            "jobs_execution_present": False,
            "jobs_journal_present": False,
            "deletion_clearance_present": False,
        },
        "postgres": {
            "container_id": "a" * 64,
            "container_name": "aap-module-data-123456abcdef",
            "acceptance_id": "b" * 24,
            "database": _DATABASE,
            "host": "127.0.0.1",
            "port": 55432,
            "database_oid": 16384,
            "cluster_system_identifier": 7451234567890123456,
            "owner_role": "owner",
        },
        "worker": {
            "container_id": "c" * 64,
            "container_name": "aap-workspace-worker-0123456789abcdef0123",
            "acceptance_id": "0123456789abcdef0123",
            "image_id": "sha256:" + "d" * 64,
            "image_acceptance_id": "9876543210abcdef98765432",
            "source_revision": "e" * 40,
            "network_id": "f" * 64,
            "network_name": "aap-workspace-worker-0123456789abcdef0123",
            "database": _DATABASE,
            "worker_started_at": "2026-10-23T00:00:00Z",
            "queue": "legacy_video_fence_0123456789abcdef0123",
            "restart_policy": "no",
            "restart_count": 0,
        },
        "worker_database_credentials": worker_credential_evidence(credentials),
        "worker_write_selectors": {
            "database_roles": sorted({item.role for item in credentials}),
            "media_control_token": "present_and_bound_to_owned_media_peer",
            "s3_principal_id": "aapcln0123456789",
            "process_secret": "present_local_worker_secret",
            "direct_routing_token": "absent",
            "direct_writer_control_token": "absent",
        },
        "storage": {
            "owner_verified": True,
            "worker_identity_matches": True,
            "user_created": True,
            "policy_created": True,
            "access_key_id": "aapcln0123456789",
            "policy_id": "aap-guide-0123456789abcdef",
            "container_id": "2" * 64,
            "bucket": "aap-wt-1234567890ab-test-guide",
            "resource_owner": "aap-worktree:1234567890abcdef:test:1234567890abcdef",
            "write_probe": deepcopy(_S3_PROBE),
        },
        "media_peer": {
            "owned_process_count": 1,
            "running": True,
            "process_id": 1001,
            "completed_materializer_process_ids": [1002],
            "endpoint": "http://127.0.0.1:19001",
        },
        "backend_peer": {
            "registry_id": "f" * 36,
            "backend_incarnation": "22234567-89ab-cdef-0123-456789abcdef",
            "management_available": True,
            "active_requests": 0,
            "endpoint": "http://127.0.0.1:19002",
        },
        "broker": {
            "queue": "legacy_video_fence_0123456789abcdef0123",
            "redis_container_id": "1" * 64,
            "redis_container_name": "aap-module-data-redis-123456abcdef",
            "redis_owner": "aap-worktree-owner-12345678",
            "redis_loopback_host": "127.0.0.1",
            "redis_loopback_port": 56379,
            "shared_unauthenticated": True,
            "external_consumers_unknown": True,
            "queue_is_fixture_specific": True,
            "queue_purge_scope": "exact_fixture_queue_only",
        },
    }


def test_worker_database_inventory_observes_all_dsns_without_serializing_secrets():
    result = inventory_worker_database_credentials(
        _environment(),
        database=_DATABASE,
        read_secret_file=lambda path: (
            _RESULT_DSN
            if path == "/run/secrets/preannotation_results_dsn"
            else pytest.fail("unexpected secret-file read")
        ),
    )

    assert {item.role for item in result} == {
        "identity_api_consumer",
        "preannotation_result_client",
        "inference_routing_client",
        "labeling_video_writer_client",
    }
    assert {item.source_variable: item.secret_file for item in result} == {
        "DATABASE_URL": False,
        "INFERENCE_ROUTING_DATABASE_URL": False,
        "LABELING_VIDEO_WRITER_DATABASE_URL": False,
        "PREANNOTATION_RESULTS_DATABASE_URL_FILE": True,
    }
    assert "worker-secret" not in repr(result)
    assert "routing-secret" not in str(worker_credential_evidence(result))


@pytest.mark.parametrize(
    "change",
    [
        lambda env: env.update(
            MIGRATION_DATABASE_URL="postgresql://owner:secret@worker-postgres/db"
        ),
        lambda env: env.update(
            ASSETS_DATABASE_URL="postgresql://assets:secret@worker-postgres/db"
        ),
        lambda env: env.pop("LABELING_VIDEO_WRITER_DATABASE_URL"),
        lambda env: env.update(
            DATABASE_URL=env["DATABASE_URL"].replace(_DATABASE, "shared")
        ),
        lambda env: env.update(
            DATABASE_URL=env["DATABASE_URL"].replace("worker-postgres", "localhost")
        ),
    ],
)
def test_worker_database_inventory_rejects_mismatched_or_incomplete_dsns(change):
    environment = _environment()
    change(environment)

    with pytest.raises(LocalFenceRefused):
        inventory_worker_database_credentials(
            environment,
            database=_DATABASE,
            read_secret_file=lambda _path: _RESULT_DSN,
        )


def test_worker_database_inventory_rejects_an_unmounted_secret_file():
    environment = _environment()
    environment["PREANNOTATION_RESULTS_DATABASE_URL_FILE"] = "/tmp/other-dsn"

    def read_only_expected_mount(path: str) -> str:
        if path != "/run/secrets/preannotation_results_dsn":
            raise LocalFenceRefused("unexpected_worker_secret_file")
        return _RESULT_DSN

    with pytest.raises(LocalFenceRefused, match="unexpected_worker_secret_file"):
        inventory_worker_database_credentials(
            environment,
            database=_DATABASE,
            read_secret_file=read_only_expected_mount,
        )


def test_worker_write_selectors_bind_only_to_owned_media_and_s3_identities():
    selectors = inspect_worker_write_selectors(
        {
            "SECRET_KEY": "process-secret",
            "ASSETS_MEDIA_TOKEN": "media-token",
            "MINIO_ACCESS_KEY": "aapcln0123456789",
            "MINIO_SECRET_KEY": "minio-secret",
            "INFERENCE_ROUTING_TOKEN": "",
            "INFERENCE_ROUTING_TOKEN_FILE": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE": "",
        },
        expected_s3_access_key="aapcln0123456789",
        expected_media_token="media-token",
    )
    assert selectors == {
        "database_roles": [
            "identity_api_consumer",
            "inference_routing_client",
            "labeling_video_writer_client",
            "preannotation_result_client",
        ],
        "media_control_token": "present_and_bound_to_owned_media_peer",
        "s3_principal_id": "aapcln0123456789",
        "process_secret": "present_local_worker_secret",
        "direct_routing_token": "absent",
        "direct_writer_control_token": "absent",
    }

    with pytest.raises(LocalFenceRefused, match="unrecognized_worker_write_selector"):
        inspect_worker_write_selectors(
            {
                "SECRET_KEY": "process-secret",
                "ASSETS_MEDIA_TOKEN": "media-token",
                "MINIO_ACCESS_KEY": "aapcln0123456789",
                "MINIO_SECRET_KEY": "minio-secret",
                "AWS_ACCESS_KEY_ID": "unknown-storage-key",
            },
            expected_s3_access_key="aapcln0123456789",
            expected_media_token="media-token",
        )


@pytest.mark.parametrize("fingerprint", ["A" * 40, "unexpected-secret"])
def test_worker_write_inventory_recognizes_only_public_gpg_fingerprints(fingerprint):
    environment = {
        "SECRET_KEY": "process-secret",
        "ASSETS_MEDIA_TOKEN": "media-token",
        "MINIO_ACCESS_KEY": "aapcln0123456789",
        "MINIO_SECRET_KEY": "minio-secret",
        "GPG_KEY": fingerprint,
    }
    if fingerprint == "A" * 40:
        assert (
            inspect_worker_write_selectors(
                environment,
                expected_s3_access_key="aapcln0123456789",
                expected_media_token="media-token",
            )["s3_principal_id"]
            == "aapcln0123456789"
        )
    else:
        with pytest.raises(
            LocalFenceRefused, match="unrecognized_worker_write_selector"
        ):
            inspect_worker_write_selectors(
                environment,
                expected_s3_access_key="aapcln0123456789",
                expected_media_token="media-token",
            )


def test_postgres_ownership_requires_exact_disposable_loopback_identity():
    record = _postgres_record()
    url = URL.create(
        "postgresql+asyncpg",
        username="owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database=_DATABASE,
    )

    identity = validate_owned_postgres_container(
        record,
        container_id="a" * 64,
        container_name="aap-module-data-123456abcdef",
        acceptance_id="b" * 24,
        host_url=url,
    )

    assert identity == {
        "container_id": "a" * 64,
        "container_name": "aap-module-data-123456abcdef",
        "acceptance_id": "b" * 24,
        "database": _DATABASE,
        "host": "127.0.0.1",
        "port": 55432,
        "image": "postgres:16-alpine",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record["Config"]["Labels"].update(
            {"org.aap.acceptance-id": "f" * 24}
        ),
        lambda record: record["NetworkSettings"]["Ports"]["5432/tcp"][0].update(
            {"HostIp": "0.0.0.0"}
        ),
        lambda record: record["State"].update({"Running": False}),
        lambda record: record["Config"].update({"Image": "postgres:latest"}),
    ],
)
def test_postgres_ownership_mismatch_refuses_before_a_mutator_can_run(mutate):
    record = _postgres_record()
    mutate(record)
    invoked = False

    def mutate_resource() -> None:
        nonlocal invoked
        invoked = True

    url = URL.create(
        "postgresql+asyncpg",
        username="owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database=_DATABASE,
    )
    with pytest.raises(LocalFenceRefused):
        validate_owned_postgres_container(
            record,
            container_id="a" * 64,
            container_name="aap-module-data-123456abcdef",
            acceptance_id="b" * 24,
            host_url=url,
        )
        mutate_resource()
    assert invoked is False


def test_worker_identity_rejects_gpu_queue_or_restartable_container():
    record = _worker_record()
    queue = "legacy_video_fence_0123456789abcdef0123"
    identity = validate_owned_worker_container(
        record,
        container_id="c" * 64,
        container_name="aap-workspace-worker-0123456789abcdef0123",
        acceptance_id="0123456789abcdef0123",
        image_id="sha256:" + "d" * 64,
        source_revision="e" * 40,
        queue=queue,
    )
    assert identity["queue"] == queue
    assert identity["restart_policy"] == "no"

    gpu_worker = deepcopy(record)
    gpu_worker["Config"]["Cmd"] = ["worker", "--queues=gpu"]
    with pytest.raises(LocalFenceRefused):
        validate_owned_worker_container(
            gpu_worker,
            container_id="c" * 64,
            container_name="aap-workspace-worker-0123456789abcdef0123",
            acceptance_id="0123456789abcdef0123",
            image_id="sha256:" + "d" * 64,
            source_revision="e" * 40,
            queue=queue,
        )

    restartable = deepcopy(record)
    restartable["HostConfig"]["RestartPolicy"]["Name"] = "always"
    with pytest.raises(LocalFenceRefused):
        validate_owned_worker_container(
            restartable,
            container_id="c" * 64,
            container_name="aap-workspace-worker-0123456789abcdef0123",
            acceptance_id="0123456789abcdef0123",
            image_id="sha256:" + "d" * 64,
            source_revision="e" * 40,
            queue=queue,
        )

    wrong_queue = "legacy_video_fence_aaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(LocalFenceRefused):
        validate_owned_worker_container(
            record,
            container_id="c" * 64,
            container_name="aap-workspace-worker-0123456789abcdef0123",
            acceptance_id="0123456789abcdef0123",
            image_id="sha256:" + "d" * 64,
            source_revision="e" * 40,
            queue=wrong_queue,
        )


@pytest.mark.parametrize("database", ["0", "1"])
def test_worker_broker_identity_uses_the_parsed_redis_database(monkeypatch, database):
    from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

    owner_url = make_url("redis://127.0.0.1:56379/0")
    monkeypatch.setenv("CELERY_BROKER_URL", str(owner_url))
    monkeypatch.setenv("AAP_MODULE_DATA_REDIS_CONTAINER_NAME", "owned-redis")
    monkeypatch.setenv("AAP_MODULE_DATA_REDIS_OWNER", "owned-redis-label")
    monkeypatch.setattr(
        "tests.test_identity_sessions_live._verify_redis", lambda: owner_url
    )
    worker_record = _worker_record()
    redis_record = {
        "Id": "1" * 64,
        "Name": "/owned-redis",
        "Config": {"Labels": {"org.aap.worktree-owner": "owned-redis-label"}},
        "NetworkSettings": {
            "Ports": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "56379"}]}
        },
    }
    worker = SimpleNamespace(
        container_id="c" * 64,
        container_name="aap-workspace-worker-0123456789abcdef0123",
        identity="0123456789abcdef0123",
        queue="legacy_video_fence_0123456789abcdef0123",
        _image_record={"id": "sha256:" + "d" * 64, "source_revision": "e" * 40},
        image_identity="9876543210abcdef98765432",
        _redis_url=f"redis://worker-redis:6379/{database}",
        _redis_owner_url=owner_url,
        _same_redis_target=WorkspaceCeleryWorker._same_redis_target,
        _inspect=lambda key: redis_record if key == "owned-redis" else worker_record,
        assert_runtime=lambda: {"started_at": worker_record["State"]["StartedAt"]},
        assert_private_network=lambda: None,
        network_id="f" * 64,
        network_name="aap-workspace-worker-0123456789abcdef0123",
        _database_name=_DATABASE,
    )
    if database == "0":
        assert owned_worker_identity(worker)["broker"]["database"] == "0"
    else:
        with pytest.raises(LocalFenceRefused, match="worker_broker_identity_mismatch"):
            owned_worker_identity(worker)


def test_fence_evidence_rejects_raw_credentials_and_connection_urls():
    safe = {
        "schema": "aap-local-video-fence/v1",
        "worker": {"container_id": "c" * 64, "queue": "legacy_video_fence_x"},
        "database_credentials": [
            {
                "source_variable": "INFERENCE_ROUTING_DATABASE_URL",
                "role": "inference_routing_client",
            }
        ],
        "broker": {
            "shared": True,
            "external_consumers_unknown": True,
            "queue_purged": False,
        },
    }
    validate_sanitized_local_fence_evidence(safe)

    with pytest.raises(LocalFenceRefused):
        validate_sanitized_local_fence_evidence(
            {**safe, "worker_dsn": "postgresql://user:secret@host/db"}
        )
    with pytest.raises(LocalFenceRefused):
        validate_sanitized_local_fence_evidence(
            {**safe, "worker": {"password": "do-not-persist"}}
        )
    with pytest.raises(LocalFenceRefused):
        validate_sanitized_local_fence_evidence(
            {**safe, "media_endpoint": "http://user:secret@127.0.0.1:9000"}
        )


def test_complete_preflight_can_run_fence_operation():
    invoked = False

    async def mutate():
        nonlocal invoked
        invoked = True
        return {"stopped": True}

    import asyncio

    result = asyncio.run(run_fence_after_preflight(_preflight(), mutate))
    assert result == {"stopped": True}
    assert invoked is True


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan["postgres"].update({"host": "shared-db.example"}),
        lambda plan: plan["postgres"].update({"acceptance_id": "shared"}),
        lambda plan: plan["worker"].update({"queue": "gpu"}),
        lambda plan: plan["worker"].update({"image_acceptance_id": ""}),
        lambda plan: plan["worker_database_credentials"].pop(),
        lambda plan: plan["scope"].update({"state": "reserved"}),
        lambda plan: plan["scope"].update({"attempt_count": 1}),
        lambda plan: plan["scope"].update({"permit_count": 1}),
        lambda plan: plan["scope"].update({"no_live_admission": False}),
        lambda plan: plan["storage"].update({"owner_verified": False}),
        lambda plan: plan["storage"]["write_probe"].update(
            {"succeeded_before_revoke": False}
        ),
        lambda plan: plan["media_peer"].update({"running": False}),
        lambda plan: plan["backend_peer"].update({"active_requests": 1}),
        lambda plan: plan["broker"].update({"redis_loopback_host": "192.0.2.1"}),
        lambda plan: plan["broker"].update({"external_consumers_unknown": False}),
    ],
)
def test_incomplete_preflight_refuses_before_any_fence_mutation(change):
    import asyncio

    plan = _preflight()
    change(plan)
    invoked = False

    async def mutate():
        nonlocal invoked
        invoked = True

    with pytest.raises(LocalFenceRefused):
        asyncio.run(run_fence_after_preflight(plan, mutate))
    assert invoked is False


def test_structured_local_fence_evidence_keeps_scope_and_broker_limits_explicit():
    observations = {
        "worker_stop": {
            "container_id": "c" * 64,
            "container_name": "aap-workspace-worker-0123456789abcdef0123",
            "acceptance_id": "0123456789abcdef0123",
            "image_id": "sha256:" + "d" * 64,
            "image_acceptance_id": "9876543210abcdef98765432",
            "source_revision": "e" * 40,
            "network_id": "f" * 64,
            "network_name": "aap-workspace-worker-0123456789abcdef0123",
            "database": _DATABASE,
            "container_removed": True,
            "started_at": "2026-10-23T00:00:00Z",
            "exit_code": 0,
            "restart_policy": "no",
            "restart_count": 0,
            "queue": "legacy_video_fence_0123456789abcdef0123",
        },
        "scope_retirement": {
            "scope_id": "11234567-89ab-cdef-0123-456789abcdef",
            "registry_id": "f" * 36,
            "retirement_id": "32234567-89ab-cdef-0123-456789abcdef",
            "authority_epoch": "42234567-89ab-cdef-0123-456789abcdef",
            "backend_incarnation": "22234567-89ab-cdef-0123-456789abcdef",
            "phase": "completed",
            "state": "reconciled",
            "closed": True,
            "scope_retired": True,
        },
        "media_peer_stop": {
            "process_exited": True,
            "endpoint_unavailable": True,
            "exit_code": 0,
        },
        "backend_peer_stop": {
            "peer_stopped": True,
            "endpoint_unavailable": True,
            "active_requests_at_stop": 0,
        },
        "database_write_fence": {
            "container_id": "a" * 64,
            "database": _DATABASE,
            "all_non_owner_logins_disabled": True,
            "all_non_owner_connect_revoked": True,
            "non_owner_logins_remaining": 0,
            "non_owner_sessions_remaining": 0,
        },
        "stale_login_denials": [
            {"role": role}
            for role in (
                "identity_api_consumer",
                "preannotation_result_client",
                "inference_routing_client",
                "labeling_video_writer_client",
            )
        ],
        "s3_revocation": {
            "access_key_id": "aapcln0123456789",
            "policy_id": "aap-guide-0123456789abcdef",
            "container_id": "2" * 64,
            "bucket": "aap-wt-1234567890ab-test-guide",
            "user_removed": True,
            "policy_removed": True,
            "old_credential_denied": True,
            "write_probe": deepcopy(_S3_PROBE),
            "denial_code": "AccessDenied",
        },
        "stale_redelivery": {
            "worker_container_id": "c" * 64,
            "worker_container_name": "aap-workspace-worker-0123456789abcdef0123",
            "worker_acceptance_id": "0123456789abcdef0123",
            "worker_image_id": "sha256:" + "d" * 64,
            "worker_image_acceptance_id": "9876543210abcdef98765432",
            "worker_source_revision": "e" * 40,
            "worker_network_id": "f" * 64,
            "worker_network_name": "aap-workspace-worker-0123456789abcdef0123",
            "worker_started_at": "2026-10-23T00:00:00Z",
            "task_state": "FAILURE",
            "worker_executed": True,
            "queue": "legacy_video_fence_0123456789abcdef0123",
            "stale_worker_db_access": False,
        },
        "stale_probe_worker_stop": {
            "container_id": "c" * 64,
            "container_name": "aap-workspace-worker-0123456789abcdef0123",
            "acceptance_id": "0123456789abcdef0123",
            "image_id": "sha256:" + "d" * 64,
            "image_acceptance_id": "9876543210abcdef98765432",
            "source_revision": "e" * 40,
            "network_id": "f" * 64,
            "network_name": "aap-workspace-worker-0123456789abcdef0123",
            "database": _DATABASE,
            "container_removed": True,
            "started_at": "2026-10-23T00:00:00Z",
            "exit_code": 0,
            "restart_policy": "no",
            "restart_count": 0,
            "queue": "legacy_video_fence_0123456789abcdef0123",
        },
        "job_state_after": {
            "business_status_unchanged": True,
            "status": "running",
            "started_at": "2026-10-23T00:00:00Z",
            "execution_absent": True,
            "journal_absent": True,
            "clearance_absent": True,
            "input_referenced": True,
            "deletion_guard_still_409": True,
        },
    }

    evidence = build_local_fence_evidence(_preflight(), observations)

    assert evidence["jobs_legacy_retirement_proof_created"] is False
    assert evidence["journal_or_clearance_created"] is False
    assert evidence["broker_boundary"] == {
        "shared_unauthenticated_broker": True,
        "external_consumers_unknown": True,
        "global_broker_isolation_proven": False,
        "only_fixture_queue_cleanup_allowed": True,
    }


def test_worker_identity_change_refuses_before_container_stop(monkeypatch):
    import tests.legacy_video_fence_helpers as fence_helpers

    current = {
        "container_id": "c" * 64,
        "container_name": "aap-workspace-worker-0123456789abcdef0123",
        "acceptance_id": "0123456789abcdef0123",
        "image_id": "sha256:" + "d" * 64,
        "source_revision": "e" * 40,
        "queue": "legacy_video_fence_0123456789abcdef0123",
        "worker_started_at": "2026-10-23T00:00:00Z",
    }

    class FakeWorker:
        container_id = current["container_id"]

        def __init__(self):
            self.docker_mutations = []

        def _docker(self, *arguments, **_kwargs):
            self.docker_mutations.append(arguments)

    worker = FakeWorker()
    monkeypatch.setattr(fence_helpers, "owned_worker_identity", lambda _worker: current)
    wrong = {**current, "image_id": "sha256:" + "f" * 64}

    with pytest.raises(LocalFenceRefused, match="worker_identity_changed_before_stop"):
        stop_owned_worker(worker, wrong)
    assert worker.docker_mutations == []
