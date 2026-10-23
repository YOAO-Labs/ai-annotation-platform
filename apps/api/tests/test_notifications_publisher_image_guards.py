from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from tests import notifications_publisher_acceptance_helpers as acceptance
from tests.notifications_publisher_acceptance_helpers import validate_publisher_image


IMAGE_ID = "sha256:" + "a" * 64
ACCEPTANCE_ID = "83334c136745f3b2b8a87852"
SOURCE_REVISION = "89b51f43e431035a0d39c92d7cb182775bd28058"
NETWORK_ID = "e" * 64


class _FakeAclOwner:
    def __init__(self, attempted):
        self.users = {}
        self.calls = []
        self.attempted = attempted

    async def acl_getuser(self, name):
        self.calls.append(("get", name))
        return self.users.get(name)

    async def execute_command(self, *arguments):
        assert arguments[2] in self.attempted
        self.calls.append(("set", arguments))
        self.users[arguments[2]] = {"created": True}

    async def acl_deluser(self, name):
        self.calls.append(("delete", name))
        return int(self.users.pop(name, None) is not None)


def _image(**labels):
    return {
        "Id": IMAGE_ID,
        "Config": {
            "User": "10004:10004",
            "Labels": {
                "org.aap.acceptance": "notifications-delivery-runtime",
                "org.aap.acceptance-id": ACCEPTANCE_ID,
                "org.aap.source-revision": SOURCE_REVISION,
                **labels,
            },
        },
    }


def _container_record(
    *,
    run_id,
    container_name,
    test_case,
    network_id,
    state,
    networks,
):
    return {
        "Id": "container-id",
        "Name": f"/{container_name}",
        "Image": IMAGE_ID,
        "Config": {
            "Labels": {
                "org.aap.acceptance": "notifications-delivery-live",
                "org.aap.acceptance-id": run_id,
                "org.aap.test-case": test_case,
            },
            "User": "10004:10004",
            "Env": [],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Memory": 268435456,
            "NanoCpus": 1000000000,
            "PidsLimit": 64,
            "NetworkMode": network_id,
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": networks},
        "State": state,
    }


def test_notifications_runtime_requires_immutable_image_and_matching_provenance():
    assert (
        validate_publisher_image(
            IMAGE_ID,
            _image(),
            acceptance_id=ACCEPTANCE_ID,
            source_revision=SOURCE_REVISION,
        )
        == IMAGE_ID
    )

    with pytest.raises(ValueError, match="immutable SHA ID"):
        validate_publisher_image(
            "notifications:latest",
            _image(),
            acceptance_id=ACCEPTANCE_ID,
            source_revision=SOURCE_REVISION,
        )

    with pytest.raises(ValueError, match="identity or labels"):
        validate_publisher_image(
            IMAGE_ID,
            _image(**{"org.aap.source-revision": "b" * 40}),
            acceptance_id=ACCEPTANCE_ID,
            source_revision=SOURCE_REVISION,
        )

    with pytest.raises(ValueError, match="identity or labels"):
        validate_publisher_image(
            IMAGE_ID,
            {
                **_image(),
                "Id": "sha256:" + "b" * 64,
            },
            acceptance_id=ACCEPTANCE_ID,
            source_revision=SOURCE_REVISION,
        )


def test_notifications_runtime_requires_expected_source_label_format():
    with pytest.raises(ValueError, match="source revision is invalid"):
        validate_publisher_image(
            IMAGE_ID,
            _image(),
            acceptance_id=ACCEPTANCE_ID,
            source_revision="not-a-commit",
        )


def test_host_acl_probe_and_container_use_separate_redis_routes():
    base_url = make_url("redis://127.0.0.1:63817/0")
    host_url, container_url = acceptance.publisher_redis_urls(
        base_url,
        username="fixture-user",
        password="fixture-password",
    )

    host = make_url(host_url)
    container = make_url(container_url)
    assert (host.host, host.port, host.username) == (
        "127.0.0.1",
        63817,
        "fixture-user",
    )
    assert (container.host, container.port, container.username) == (
        "notifications-redis",
        6379,
        "fixture-user",
    )


@pytest.mark.asyncio
async def test_scoped_acl_creation_checks_absence_and_cleanup_is_idempotent():
    attempted = []
    owner = _FakeAclOwner(attempted)

    await acceptance.create_publisher_acl_user(
        owner,
        "notify-test-user",
        "fixture-password",
        allow_publish=False,
        attempted=attempted,
    )

    assert attempted == ["notify-test-user"]
    assert owner.calls[0] == ("get", "notify-test-user")
    set_arguments = owner.calls[1][1]
    assert set_arguments[:3] == ("ACL", "SETUSER", "notify-test-user")
    assert "reset" not in set_arguments and "+publish" not in set_arguments

    await acceptance.delete_attempted_publisher_acl_user(owner, "notify-test-user")
    await acceptance.delete_attempted_publisher_acl_user(owner, "notify-test-user")
    assert owner.users == {}
    assert [call[0] for call in owner.calls].count("delete") == 1


@pytest.mark.asyncio
async def test_preexisting_acl_user_is_never_reset_or_registered_for_cleanup():
    attempted = []
    owner = _FakeAclOwner(attempted)
    owner.users["notify-preexisting"] = {"created": False}

    with pytest.raises(pytest.fail.Exception):
        await acceptance.create_publisher_acl_user(
            owner,
            "notify-preexisting",
            "fixture-password",
            allow_publish=True,
            attempted=attempted,
        )

    assert attempted == []
    assert owner.calls == [("get", "notify-preexisting")]
    assert owner.users["notify-preexisting"] == {"created": False}


def test_default_container_command_uses_the_installed_publisher_cli(monkeypatch):
    arguments_seen = []
    monkeypatch.setattr(
        acceptance,
        "_docker",
        lambda *arguments, **_kwargs: (
            arguments_seen.extend(arguments) or "container-id"
        ),
    )
    monkeypatch.setattr(
        acceptance, "verify_live_container", lambda *_args, **_kwargs: {}
    )

    container_id = acceptance.create_live_container(
        image_id=IMAGE_ID,
        run_id=ACCEPTANCE_ID,
        network_name="aap-notify-live-test",
        network_id=NETWORK_ID,
        case="default-command",
    )

    assert container_id == "container-id"
    assert arguments_seen[-3:] == [
        IMAGE_ID,
        "aap-notifications-publisher",
        "--once",
    ]
    assert "--entrypoint" not in arguments_seen
    assert arguments_seen[arguments_seen.index("--network") + 1] == NETWORK_ID


def test_listener_command_still_overrides_entrypoint_explicitly():
    assert acceptance.notifications_container_command(
        entrypoint="python",
        command=("-c", "pass"),
    ) == ("-c", "pass")


def test_private_network_mutations_use_inspected_network_id(monkeypatch):
    network_name = "aap-notify-live-owned"
    network_id = "f" * 64
    dependency_id = "d" * 64
    run_id = "e" * 24
    endpoint = {
        "NetworkID": network_id,
        "EndpointID": "endpoint-id",
        "Aliases": ["notifications-postgres"],
    }
    endpoint_results = [endpoint, endpoint, None]
    calls = []

    def docker(*arguments, **_kwargs):
        calls.append(arguments)
        return ""

    def inspect(kind, target):
        if kind == "network":
            assert target == network_id
            return {
                "Name": network_name,
                "Id": network_id,
                "Labels": {
                    "org.aap.acceptance": "notifications-delivery-live",
                    "org.aap.acceptance-id": run_id,
                },
                "Containers": {},
            }
        observed_endpoint = endpoint_results.pop(0)
        networks = {network_name: observed_endpoint} if observed_endpoint else {}
        return {"NetworkSettings": {"Networks": networks}}

    monkeypatch.setattr(acceptance, "_docker", docker)
    monkeypatch.setattr(acceptance, "inspect_docker", inspect)

    acceptance.connect_owned_dependency(
        network_name, network_id, dependency_id, "notifications-postgres"
    )
    acceptance.disconnect_owned_dependency(network_name, network_id, dependency_id)
    acceptance.remove_owned_network(network_name, network_id, run_id)

    assert calls == [
        (
            "network",
            "connect",
            "--alias",
            "notifications-postgres",
            network_id,
            dependency_id,
        ),
        ("network", "disconnect", network_id, dependency_id),
        ("network", "rm", network_id),
    ]


def test_container_endpoint_must_match_the_immutable_network_id(monkeypatch):
    network_name = "aap-notify-live-owned"
    observed = _container_record(
        run_id=ACCEPTANCE_ID,
        container_name="aap-notify-83334c1367-cold-publisher",
        test_case="cold-publisher",
        network_id=NETWORK_ID,
        state={"Status": "running", "Running": True, "Pid": 123},
        networks={network_name: {"NetworkID": "wrong-network-id"}},
    )
    monkeypatch.setattr(acceptance, "inspect_docker", lambda *_args: observed)

    with pytest.raises(pytest.fail.Exception):
        acceptance.verify_live_container(
            "container-id",
            run_id=ACCEPTANCE_ID,
            image_id=IMAGE_ID,
            network_name=network_name,
            network_id=NETWORK_ID,
            phase="running",
        )


def test_created_docker_container_uses_network_mode_before_endpoint_exists(monkeypatch):
    network_name = "aap-notify-live-owned"
    container_name = f"aap-notify-{ACCEPTANCE_ID[:10]}-cold-publisher"
    record = _container_record(
        run_id=ACCEPTANCE_ID,
        container_name=container_name,
        test_case="cold-publisher",
        network_id=NETWORK_ID,
        state={"Status": "created", "Running": False, "Pid": 0},
        networks={network_name: {"NetworkID": "", "EndpointID": ""}},
    )
    commands = []
    monkeypatch.setattr(acceptance, "inspect_docker", lambda *_args: record)
    monkeypatch.setattr(
        acceptance,
        "_docker",
        lambda *arguments, **_kwargs: commands.append(arguments) or "",
    )

    acceptance.verify_live_container(
        "container-id",
        run_id=ACCEPTANCE_ID,
        image_id=IMAGE_ID,
        network_name=network_name,
        network_id=NETWORK_ID,
        phase="created",
        container_name=container_name,
        test_case="cold-publisher",
        secret_destinations=(),
    )
    acceptance.remove_owned_container(
        "container-id",
        run_id=ACCEPTANCE_ID,
        network_name=network_name,
        network_id=NETWORK_ID,
    )

    assert commands == [("rm", "container-id")]


@pytest.mark.parametrize("phase", ["started", "stopped"])
def test_exited_container_keeps_network_id_without_endpoint_id(monkeypatch, phase):
    network_name = "aap-notify-live-owned"
    container_name = f"aap-notify-{ACCEPTANCE_ID[:10]}-cold-publisher"
    record = _container_record(
        run_id=ACCEPTANCE_ID,
        container_name=container_name,
        test_case="cold-publisher",
        network_id=NETWORK_ID,
        state={"Status": "exited", "Running": False, "Pid": 0},
        networks={network_name: {"NetworkID": NETWORK_ID, "EndpointID": ""}},
    )
    monkeypatch.setattr(acceptance, "inspect_docker", lambda *_args: record)

    result = acceptance.verify_live_container(
        "container-id",
        run_id=ACCEPTANCE_ID,
        image_id=IMAGE_ID,
        network_name=network_name,
        network_id=NETWORK_ID,
        phase=phase,
        container_name=container_name,
        test_case="cold-publisher",
    )

    assert (
        result["NetworkSettings"]["Networks"][network_name]["NetworkID"] == NETWORK_ID
    )


def test_network_create_timeout_cleans_only_exact_owned_partial(monkeypatch):
    run_id = "a" * 24
    name = f"aap-notify-live-{run_id[:12]}"
    removed = []

    def docker(*arguments, **_kwargs):
        if arguments[:2] == ("network", "create"):
            raise subprocess.TimeoutExpired("docker network create", 30)
        if arguments[:2] == ("network", "rm"):
            removed.append(arguments[-1])
            return ""
        raise AssertionError("unexpected Docker command")

    monkeypatch.setattr(acceptance, "_docker", docker)
    monkeypatch.setattr(acceptance, "_named_object_exists", lambda *_args: True)
    monkeypatch.setattr(
        acceptance,
        "inspect_docker",
        lambda *_args: {
            "Name": name,
            "Id": "network-id",
            "Internal": True,
            "Labels": {
                "org.aap.acceptance": "notifications-delivery-live",
                "org.aap.acceptance-id": run_id,
            },
            "Containers": {},
        },
    )

    with pytest.raises(subprocess.TimeoutExpired):
        acceptance.create_private_network(run_id)

    assert removed == ["network-id"]


def test_network_partial_cleanup_failure_is_not_swallowed(monkeypatch):
    run_id = "c" * 24
    name = f"aap-notify-live-{run_id[:12]}"
    cleanup_error = RuntimeError("owned network cleanup failed")

    def docker(*arguments, **_kwargs):
        if arguments[:2] == ("network", "create"):
            raise subprocess.TimeoutExpired("docker network create", 30)
        if arguments[:2] == ("network", "rm"):
            raise cleanup_error
        raise AssertionError("unexpected Docker command")

    monkeypatch.setattr(acceptance, "_docker", docker)
    monkeypatch.setattr(acceptance, "_named_object_exists", lambda *_args: True)
    monkeypatch.setattr(
        acceptance,
        "inspect_docker",
        lambda *_args: {
            "Name": name,
            "Id": "network-id",
            "Internal": True,
            "Labels": {
                "org.aap.acceptance": "notifications-delivery-live",
                "org.aap.acceptance-id": run_id,
            },
            "Containers": {},
        },
    )

    with pytest.raises(RuntimeError, match="owned network cleanup failed"):
        acceptance.create_private_network(run_id)


def test_container_create_timeout_cleans_exact_owned_partial(monkeypatch):
    run_id = "d" * 24
    name = f"aap-notify-{run_id[:10]}-partial"
    removed = []
    monkeypatch.setattr(
        acceptance,
        "_docker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("docker create", 30)
        ),
    )
    monkeypatch.setattr(acceptance, "_named_object_exists", lambda *_args: True)
    monkeypatch.setattr(
        acceptance,
        "inspect_docker",
        lambda *_args: _container_record(
            run_id=run_id,
            container_name=name,
            test_case="partial",
            network_id=NETWORK_ID,
            state={"Status": "created", "Running": False, "Pid": 0},
            networks={"aap-notify-live-test": {"NetworkID": "", "EndpointID": ""}},
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "remove_owned_container",
        lambda container_id, **kwargs: removed.append((container_id, kwargs)),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        acceptance.create_live_container(
            image_id=IMAGE_ID,
            run_id=run_id,
            network_name="aap-notify-live-test",
            network_id=NETWORK_ID,
            case="partial",
        )

    assert removed == [
        (
            "container-id",
            {
                "run_id": run_id,
                "network_name": "aap-notify-live-test",
                "network_id": NETWORK_ID,
            },
        )
    ]


def test_partial_container_cleanup_failure_is_not_swallowed(monkeypatch):
    run_id = "b" * 24
    name = f"aap-notify-{run_id[:10]}-partial"
    monkeypatch.setattr(
        acceptance,
        "_docker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("docker create", 30)
        ),
    )
    monkeypatch.setattr(acceptance, "_named_object_exists", lambda *_args: True)
    monkeypatch.setattr(
        acceptance,
        "inspect_docker",
        lambda *_args: _container_record(
            run_id=run_id,
            container_name=name,
            test_case="partial",
            network_id=NETWORK_ID,
            state={"Status": "created", "Running": False, "Pid": 0},
            networks={"aap-notify-live-test": {"NetworkID": "", "EndpointID": ""}},
        ),
    )
    cleanup_error = RuntimeError("owned container cleanup failed")
    monkeypatch.setattr(
        acceptance,
        "remove_owned_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cleanup_error),
    )

    with pytest.raises(RuntimeError, match="owned container cleanup failed"):
        acceptance.create_live_container(
            image_id=IMAGE_ID,
            run_id=run_id,
            network_name="aap-notify-live-test",
            network_id=NETWORK_ID,
            case="partial",
        )


def test_partial_secret_files_are_unlinked_from_the_exact_private_directory(tmp_path):
    secret_directory = tmp_path / "notifications-publisher-secrets-partial"
    secret_directory.mkdir(mode=0o700)
    database_url = secret_directory / "database-url"
    database_url.write_text("postgresql://fixture-secret", encoding="utf-8")
    database_url.chmod(0o444)

    acceptance.remove_secret_files(tmp_path, secret_directory)

    assert not database_url.exists()
    assert not secret_directory.exists()


def test_owned_container_audit_requests_full_docker_ids(monkeypatch):
    run_id = "a" * 24
    full_id = "f" * 64
    labels = {
        "org.aap.acceptance": "notifications-delivery-live",
        "org.aap.acceptance-id": run_id,
    }
    commands = []

    def docker(arguments, **_kwargs):
        commands.append(arguments)
        listed_id = full_id if "--no-trunc" in arguments else full_id[:12]
        return SimpleNamespace(returncode=0, stdout=listed_id + "\n", stderr="")

    monkeypatch.setattr(acceptance.subprocess, "run", docker)
    monkeypatch.setattr(
        acceptance,
        "inspect_docker",
        lambda *_args: {
            "Id": full_id,
            "Config": {"Labels": labels},
        },
    )

    with pytest.raises(pytest.fail.Exception, match="left an owned container"):
        acceptance.assert_no_owned_containers(run_id)

    assert "--no-trunc" in commands[0]


@pytest.mark.asyncio
async def test_cleanup_failure_is_captured_so_later_owned_cleanup_runs():
    completed = []

    async def rejected_cleanup():
        pytest.fail("owned cleanup rejected")

    async def next_cleanup():
        completed.append(True)

    failure = await acceptance.run_owned_cleanup_step(rejected_cleanup)
    next_failure = await acceptance.run_owned_cleanup_step(next_cleanup)

    assert isinstance(failure, pytest.fail.Exception)
    assert next_failure is None
    assert completed == [True]
