"""Pure guards for the Jobs cold-assembly fixture inputs."""

from __future__ import annotations

import json
import os
import uuid
import pytest

from tests.identity_module_assembly_helpers import (
    IdentityModuleStack,
    _expected_secret_mount_names,
    validate_jobs_queue_broker_url,
    validate_jobs_queue_image_inputs,
)
from tests.test_jobs_module_assembly_live import (
    _JOBS_API_ROW_ACCESS_PROBE,
    _assert_api_row_access_probe,
)
from tests.workspace_worker_assembly_helpers import _worker_queue_name


@pytest.mark.parametrize(
    "broker", ["redis://127.0.0.1:46379/0", "redis://worker:secret@127.0.0.1:46379/0"]
)
def test_jobs_broker_accepts_owned_authenticated_or_password_free_local_target(broker):
    assert (
        validate_jobs_queue_broker_url(broker, "redis://127.0.0.1:46379/0", "token")
        == broker
    )


@pytest.mark.parametrize(
    "broker,token",
    [
        ("redis://other:46379/0", "token"),
        ("redis://127.0.0.1:46379/1", "token"),
        ("redis://worker:token@127.0.0.1:46379/0", "token"),
    ],
)
def test_jobs_broker_rejects_foreign_target_or_reused_bearer(broker, token):
    with pytest.raises(ValueError):
        validate_jobs_queue_broker_url(broker, "redis://127.0.0.1:46379/0", token)


def test_restored_role_context_keeps_the_existing_database_guard(monkeypatch):
    from sqlalchemy.engine import make_url
    from tests.test_jobs_module_assembly_live import _role_urls_for_database
    from tests.test_module_data_roles import _role_url

    reference = "postgresql+asyncpg://owner:fixture@127.0.0.1:46379/source_test"
    variable = "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"
    role = "postgresql+asyncpg://preannotation_result_client:fixture@127.0.0.1:46379/source_test"
    monkeypatch.setenv("TEST_DATABASE_URL", reference)
    monkeypatch.setenv(variable, role)
    with _role_urls_for_database("restored_test"):
        selected = _role_url(variable, "preannotation_result_client")
        assert make_url(selected).database == "restored_test"
        assert make_url(os.environ["TEST_DATABASE_URL"]).database == "restored_test"
    assert os.environ["TEST_DATABASE_URL"] == reference
    assert os.environ[variable] == role


def test_jobs_queue_image_requires_complete_source_bound_inputs():
    assert not validate_jobs_queue_image_inputs(None, None, None, None)
    assert validate_jobs_queue_image_inputs(
        "sha256:" + "a" * 64,
        "b" * 24,
        "c" * 40,
        "jobs-queue-fixture-token",
    )


@pytest.mark.parametrize(
    "values",
    [
        ("sha256:" + "a" * 64, "b" * 24, "c" * 40, None),
        ("sha256:" + "a" * 64, "b" * 24, "c" * 39, "token"),
        ("sha256:" + "a" * 63, "b" * 24, "c" * 40, "token"),
        ("sha256:" + "a" * 64, "b" * 23, "c" * 40, "token"),
        ("sha256:" + "a" * 64, "b" * 24, "c" * 40, "bad token"),
        ("sha256:" + "a" * 64, "b" * 24, "c" * 40, "令牌"),
    ],
)
def test_jobs_queue_image_rejects_incomplete_or_ambiguous_inputs(values):
    with pytest.raises(ValueError):
        validate_jobs_queue_image_inputs(*values)


def test_jobs_queue_secret_mounts_are_opt_in_and_service_scoped():
    assert (
        _expected_secret_mount_names("jobs-queue", notifications_socket_enabled=False)
        is None
    )
    assert _expected_secret_mount_names(
        "jobs-queue",
        notifications_socket_enabled=False,
        jobs_queue_enabled=True,
    ) == {"jobs_queue_broker_url", "jobs_queue_token"}
    assert _expected_secret_mount_names(
        "identity-api",
        notifications_socket_enabled=False,
        jobs_queue_enabled=True,
    ) == {"jobs_queue_token"}


def test_jobs_queue_compose_override_is_private_and_uses_only_service_secrets(
    tmp_path,
):
    stack = object.__new__(IdentityModuleStack)
    stack._api_env_file = tmp_path / "api.env"
    stack._override_file = tmp_path / "override.yml"
    stack._notification_publication_enabled = False
    stack._notifications_socket_enabled = False
    stack._jobs_queue_enabled = True

    stack._write_override_file()
    rendered = stack._override_file.read_text(encoding="utf-8")
    queue_service = rendered.split("  jobs-queue:", 1)[1].split("\n  identity-api:", 1)[
        0
    ]
    api_service = rendered.split("  identity-api:", 1)[1].split("\nsecrets:", 1)[0]

    assert 'user: "10005:10005"' in queue_service
    assert "read_only: true" in queue_service
    assert "cap_drop: [ALL]" in queue_service
    assert (
        "JOBS_QUEUE_BROKER_URL_FILE: /run/secrets/jobs_queue_broker_url"
        in queue_service
    )
    assert "JOBS_QUEUE_TOKEN_FILE: /run/secrets/jobs_queue_token" in queue_service
    assert "ports:" not in queue_service
    assert "jobs_queue_broker_url" in queue_service
    assert "jobs_queue_token" in queue_service
    assert "jobs_queue_token" in api_service
    assert "jobs_queue_broker_url" not in api_service


def test_workspace_worker_only_allows_explicit_default_queue_opt_in():
    assert _worker_queue_name("owned-worker") == "aap_workspace_worker_owned-worker"
    assert _worker_queue_name("owned-worker", "default") == "default"
    with pytest.raises(ValueError):
        _worker_queue_name("owned-worker", "some-other-queue")


def _row_access_probe_evidence(**changes):
    return {
        "ok": True,
        "protocols": ["aap-jobs-lifecycle/v1", "aap-jobs-row-locks/v1"],
        "role_match": True,
        "database_match": True,
        "function_calls": {
            "read_job": 1,
            "read_job_for_share_nowait": 1,
            "read_job_for_update": 1,
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
    assert stack.command[:5] == ["exec", "owned-api", "python", "-I", "-c"]
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
