"""Pure guards for the Jobs cold-assembly fixture inputs."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import pytest
from sqlalchemy.engine import make_url

from tests.identity_module_assembly_helpers import (
    IdentityModuleStack,
    _expected_secret_mount_names,
)
from tests import test_jobs_module_assembly_live as assembly
from tests.test_jobs_module_assembly_live import (
    _JOBS_API_ROW_ACCESS_PROBE,
    _assert_api_row_access_probe,
)
from tests.workspace_worker_assembly_helpers import _worker_queue_name


@pytest.mark.parametrize(
    ("broker", "owned"),
    [
        ("redis://127.0.0.1:46379/0", True),
        ("redis://worker:secret@127.0.0.1:46379/0", True),
        ("redis://other:46379/0", False),
        ("redis://127.0.0.1:46379/1", False),
    ],
)
def test_embedded_sender_guard_checks_owned_redis_before_examining_queue(
    monkeypatch, broker, owned
):
    from types import SimpleNamespace

    examined = []
    monkeypatch.setenv("CELERY_BROKER_URL", broker)
    monkeypatch.setattr(
        assembly, "_verify_redis", lambda: make_url("redis://127.0.0.1:46379/0")
    )

    def state(_sender, queue):
        examined.append(queue)
        return {"queue_size": 0, "unacknowledged": 0}

    monkeypatch.setattr(assembly, "_broker_state", state)
    if owned:
        assembly._assert_owned_broker_target(SimpleNamespace(), "export")
        assert examined == ["export"]
    else:
        with pytest.raises(AssertionError, match="owned Redis"):
            assembly._assert_owned_broker_target(SimpleNamespace(), "export")
        assert examined == []


@pytest.mark.parametrize(
    "variable,role_name",
    [
        (
            "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL",
            "yoao_preannotation_result_client",
        ),
        ("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL", "yoao_asset_media_control_client"),
        ("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL", "yoao_asset_media_worker_client"),
        ("AAP_MODULE_DATA_INFERENCE_ROUTING_URL", "yoao_inference_routing_client"),
        (
            "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL",
            "yoao_labeling_video_writer_client",
        ),
        ("AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL", "yoao_jobs_dispatch_relay_client"),
        ("AAP_MODULE_DATA_JOBS_PUBLICATION_URL", "yoao_jobs_publication_client"),
    ],
)
def test_restored_role_context_keeps_the_existing_database_guard(
    monkeypatch, variable, role_name
):
    from sqlalchemy.engine import make_url
    from tests.test_jobs_module_assembly_live import _role_urls_for_database
    from tests.test_module_data_roles import _role_url

    reference = "postgresql+asyncpg://owner:fixture@127.0.0.1:46379/source_test"
    role = f"postgresql+asyncpg://{role_name}:fixture@127.0.0.1:46379/source_test"
    monkeypatch.setenv("TEST_DATABASE_URL", reference)
    monkeypatch.setenv(variable, role)
    with _role_urls_for_database("restored_test"):
        selected = _role_url(variable, role_name)
        assert make_url(selected).database == "restored_test"
        assert make_url(os.environ["TEST_DATABASE_URL"]).database == "restored_test"
    assert os.environ["TEST_DATABASE_URL"] == reference
    assert os.environ[variable] == role


def test_stack_destruction_waits_for_every_owner_guard():
    stack = object.__new__(IdentityModuleStack)
    stack._retention_guards = []
    states = {"export": True, "tracker": False}
    stack.retain_while(lambda: states["export"])
    stack.retain_while(lambda: states["tracker"])

    class ForbiddenPath:
        def is_file(self):
            raise AssertionError("retained stack attempted destruction")

    stack._compose_env_file = ForbiddenPath()
    assert stack.retention_required
    with pytest.raises(RuntimeError, match="owner retirement is unproved"):
        asyncio.run(stack.stop())
    with pytest.raises(RuntimeError, match="owner retirement is unproved"):
        asyncio.run(stack.switch_database("foreign_test"))
    states["export"] = False
    states["tracker"] = True
    assert stack.retention_required
    with pytest.raises(RuntimeError, match="owner retirement is unproved"):
        asyncio.run(stack.stop())
    states["tracker"] = False
    assert not stack.retention_required

    def unknown_guard():
        raise RuntimeError("owner proof unavailable")

    stack.retain_while(unknown_guard)
    with pytest.raises(RuntimeError, match="owner proof unavailable"):
        asyncio.run(stack.stop())


def test_jobs_publication_secret_mount_is_only_for_cold_api():
    base = _expected_secret_mount_names("identity-api")
    producer = _expected_secret_mount_names("identity-api", jobs_publication=True)
    assert producer - base == {"jobs_publication_database_url"}
    assert "jobs_dispatch_database_url" not in producer
    assert _expected_secret_mount_names("identity-issuer", jobs_publication=True) == (
        _expected_secret_mount_names("identity-issuer")
    )
    assert _expected_secret_mount_names("jobs-queue", jobs_publication=True) is None


def test_workspace_worker_only_allows_explicit_qualified_queue_opt_in():
    assert _worker_queue_name("owned-worker") == "aap_workspace_worker_owned-worker"
    assert _worker_queue_name("owned-worker", "default") == "default"
    assert _worker_queue_name("owned-worker", "export") == "export"
    assert _worker_queue_name("owned-worker", "media") == "media"
    with pytest.raises(ValueError):
        _worker_queue_name("owned-worker", "gpu")
    with pytest.raises(ValueError):
        _worker_queue_name("owned-worker", "some-other-queue")


def _row_access_probe_evidence(**changes):
    return {
        "ok": True,
        "protocols": [
            "aap-jobs-lifecycle/v1",
            "aap-jobs-row-locks/v1",
            "aap-jobs-domain-queries/v1",
            "aap-jobs-fixture-cleanup/v1",
            "aap-jobs-filter-fixtures/v1",
            "aap-jobs-action-idempotency/v1",
        ],
        "role_match": True,
        "database_match": True,
        "function_calls": {
            "read_job": 3,
            "read_job_for_share_nowait": 1,
            "read_job_for_update": 1,
        },
        "domain_function_calls": {
            "lock_cross_frame_singleflight": 1,
            "active_cross_frame_job": 1,
            "cross_frame_job_page": 1,
            "cross_frame_job_count": 1,
            "latest_batch_prediction_times": 1,
        },
        "domain_subject_matches": True,
        "action_function_calls": {
            "lock_action_idempotency": 1,
            "find_action_idempotent_jobs": 1,
        },
        "action_identity_and_digest": True,
        "point_cloud_projection": True,
        "creation_acl": {
            "private_table_insert": False,
            "private_column_insert": False,
            "creation_view_select": True,
            "creation_view_insert": True,
            "pending_shape": True,
            "private_insert_sqlstate": "42501",
            "caller_rollback": True,
        },
        "fixture_delete_acl": {
            "private_table_delete": False,
            "private_delete_sqlstate": "42501",
            "fixture_delete_sqlstate": "42501",
            "pending_row_preserved": True,
            "completed_row_restored": True,
            "caller_rollback": True,
        },
        "fixture_update_acl": {
            "private_table_update": False,
            "private_column_update": False,
            "private_update_sqlstate": "42501",
            "lifecycle_projection_and_expiry": True,
            "fixture_create_sqlstate": "42501",
            "caller_rollback": True,
        },
        "private_table_from": 0,
        "partial_refresh": True,
        "same_orm_identity": True,
        **changes,
    }


class _RowAccessProbeStack:
    def __init__(self, evidence, *, actual_container_id="owned-api"):
        self.evidence = evidence
        self.actual_container_id = actual_container_id
        self.service_ids = {"identity-api": "owned-api"}
        self.calls = []
        self.command = None

    def _verify_project_ownership(self):
        self.calls.append("verify")
        return {"identity-api": {"Id": self.actual_container_id}}

    def _docker(self, arguments, *, operation, timeout):
        self.calls.append("docker")
        self.command = arguments
        self.operation = operation
        self.timeout = timeout
        return json.dumps(self.evidence)

    @staticmethod
    def redact(value):
        return value


def test_api_row_access_probe_execs_only_verified_container_with_isolated_python():
    compile(_JOBS_API_ROW_ACCESS_PROBE, "<jobs row-access probe>", "exec")
    stack = _RowAccessProbeStack(_row_access_probe_evidence())
    job_id = uuid.uuid4()

    evidence = _assert_api_row_access_probe(
        stack, job_id=job_id, database="restored_test"
    )

    assert stack.calls == ["verify", "docker"]
    assert stack.command[:5] == [
        "exec",
        "owned-api",
        "/opt/venv/bin/python",
        "-I",
        "-c",
    ]
    assert stack.command[5] == _JOBS_API_ROW_ACCESS_PROBE
    assert stack.command[6:] == [str(job_id), "restored_test"]
    assert stack.operation == "Jobs row-access probe"
    assert stack.timeout == 60
    assert evidence["private_table_from"] == 0


def test_api_row_access_probe_rejects_unowned_container_before_exec():
    stack = _RowAccessProbeStack(
        _row_access_probe_evidence(), actual_container_id="foreign-api"
    )

    with pytest.raises(AssertionError, match="owned API container"):
        _assert_api_row_access_probe(
            stack, job_id=uuid.uuid4(), database="restored_test"
        )

    assert stack.calls == ["verify"]


def test_api_row_access_probe_rejects_private_table_reads():
    stack = _RowAccessProbeStack(_row_access_probe_evidence(private_table_from=1))

    with pytest.raises(AssertionError):
        _assert_api_row_access_probe(
            stack, job_id=uuid.uuid4(), database="restored_test"
        )

    assert stack.calls == ["verify", "docker"]


@pytest.mark.parametrize("field", ["private_table_insert", "private_column_insert"])
def test_api_row_access_probe_rejects_private_job_insert_privileges(field):
    evidence = _row_access_probe_evidence()
    evidence["creation_acl"][field] = True
    stack = _RowAccessProbeStack(evidence)
    with pytest.raises(AssertionError):
        _assert_api_row_access_probe(
            stack, job_id=uuid.uuid4(), database="restored_test"
        )
    assert stack.calls == ["verify", "docker"]


@pytest.mark.parametrize(
    "field,value", [("private_table_delete", True), ("completed_row_restored", False)]
)
def test_api_row_access_probe_rejects_missing_delete_boundary_evidence(field, value):
    evidence = _row_access_probe_evidence()
    evidence["fixture_delete_acl"][field] = value
    stack = _RowAccessProbeStack(evidence)
    with pytest.raises(AssertionError):
        _assert_api_row_access_probe(
            stack, job_id=uuid.uuid4(), database="restored_test"
        )


@pytest.mark.parametrize("field", ["private_table_update", "private_column_update"])
def test_api_row_access_probe_rejects_private_job_update_privileges(field):
    evidence = _row_access_probe_evidence()
    evidence["fixture_update_acl"][field] = True
    with pytest.raises(AssertionError):
        _assert_api_row_access_probe(
            _RowAccessProbeStack(evidence),
            job_id=uuid.uuid4(),
            database="restored_test",
        )


@pytest.mark.parametrize(
    "field", ["action_identity_and_digest", "point_cloud_projection"]
)
def test_api_row_access_probe_rejects_missing_action_or_projection_evidence(field):
    with pytest.raises(AssertionError):
        _assert_api_row_access_probe(
            _RowAccessProbeStack(_row_access_probe_evidence(**{field: False})),
            job_id=uuid.uuid4(),
            database="restored_test",
        )
