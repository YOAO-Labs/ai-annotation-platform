"""Pure guards for the Jobs cold-assembly fixture inputs."""

from __future__ import annotations

import pytest

from tests.identity_module_assembly_helpers import (
    IdentityModuleStack,
    _expected_secret_mount_names,
    validate_jobs_queue_image_inputs,
)
from tests.workspace_worker_assembly_helpers import _worker_queue_name


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
