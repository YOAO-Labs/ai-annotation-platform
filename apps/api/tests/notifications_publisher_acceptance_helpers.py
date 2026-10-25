"""Small, ownership-checked Docker helpers for the Notifications live case."""

from __future__ import annotations

import json
import inspect
from pathlib import Path
import re
import subprocess
import time

import pytest


_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{24}\Z")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_ACCEPTANCE = "notifications-delivery-runtime"
_CONTAINER_ACCEPTANCE = "notifications-delivery-live"


def validate_publisher_image(
    image_reference: str,
    image: dict,
    *,
    acceptance_id: str,
    source_revision: str,
) -> str:
    """Require a pinned image and matching acceptance provenance labels."""
    if _IMAGE_ID.fullmatch(image_reference) is None:
        raise ValueError("Notifications image reference must be an immutable SHA ID")
    if _ACCEPTANCE_ID.fullmatch(acceptance_id) is None:
        raise ValueError("Notifications image acceptance ID is invalid")
    if _SOURCE_REVISION.fullmatch(source_revision) is None:
        raise ValueError("Notifications image source revision is invalid")

    image_id = image.get("Id")
    labels = image.get("Config", {}).get("Labels") or {}
    if not (
        image_id == image_reference
        and labels.get("org.aap.acceptance") == _IMAGE_ACCEPTANCE
        and labels.get("org.aap.acceptance-id") == acceptance_id
        and labels.get("org.aap.source-revision") == source_revision
        and image.get("Config", {}).get("User") == "10004:10004"
    ):
        raise ValueError("Notifications image identity or labels do not match")
    return image_id


def _docker(*arguments: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("owned Notifications Docker operation failed")
    return result.stdout.strip()


async def run_owned_cleanup_step(operation):
    """Capture one owned cleanup failure so later steps can still run."""
    try:
        result = operation()
        if inspect.isawaitable(result):
            await result
    except BaseException as error:
        return error
    return None


def inspect_docker(kind: str, target: str) -> dict:
    try:
        values = json.loads(_docker(kind, "inspect", target, timeout=15))
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        pytest.fail("Docker returned an invalid Notifications ownership record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("Notifications Docker ownership record was not unique")
    return values[0]


def inspect_publisher_image(
    image_reference: str,
    *,
    acceptance_id: str,
    source_revision: str,
) -> dict:
    image = inspect_docker("image", image_reference)
    try:
        image_id = validate_publisher_image(
            image_reference,
            image,
            acceptance_id=acceptance_id,
            source_revision=source_revision,
        )
    except ValueError as error:
        pytest.fail(str(error))
    return {
        "id": image_id,
        "acceptance_id": acceptance_id,
        "source_revision": source_revision,
    }


def publisher_redis_urls(base_url, *, username: str, password: str) -> tuple[str, str]:
    """Return host-side ACL proof URL and private-network container URL."""
    host_url = base_url.set(
        username=username,
        password=password,
    ).render_as_string(hide_password=False)
    container_url = base_url.set(
        host="notifications-redis",
        port=6379,
        username=username,
        password=password,
    ).render_as_string(hide_password=False)
    return host_url, container_url


async def create_publisher_acl_user(owner, name, password, *, allow_publish, attempted):
    if await owner.acl_getuser(name) is not None:
        pytest.fail("Generated Notifications Redis ACL name already exists")
    attempted.append(name)
    commands = [
        "-@all",
        "+ping",
        "+select",
        "+client|setinfo",
        "+client|setname",
    ]
    if allow_publish:
        commands.append("+publish")
    await owner.execute_command(
        "ACL",
        "SETUSER",
        name,
        "on",
        ">" + password,
        "&notify:*",
        *commands,
    )


async def delete_attempted_publisher_acl_user(owner, name):
    if await owner.acl_getuser(name) is not None:
        await owner.acl_deluser(name)


def notifications_container_command(
    *,
    entrypoint: str | None,
    command: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Resolve Docker CMD semantics for the publisher and its test listener."""
    if entrypoint is None:
        return ("aap-notifications-publisher", *(command or ("--once",)))
    return command or ()


def verify_live_container(
    container_id: str,
    *,
    run_id: str,
    image_id: str,
    network_name: str,
    network_id: str,
    phase: str = "running",
    container_name: str | None = None,
    test_case: str | None = None,
    secret_destinations: tuple[str, ...] = (),
) -> dict:
    observed = inspect_docker("container", container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    host = observed.get("HostConfig", {})
    config = observed.get("Config", {})
    mounts = observed.get("Mounts", [])
    networks = observed.get("NetworkSettings", {}).get("Networks", {})
    observed_mounts = {
        mount.get("Destination"): mount for mount in mounts if mount.get("Destination")
    }
    if not (
        observed.get("Id") == container_id
        and observed.get("Image") == image_id
        and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
        and labels.get("org.aap.acceptance-id") == run_id
        and (test_case is None or labels.get("org.aap.test-case") == test_case)
        and (container_name is None or observed.get("Name") == f"/{container_name}")
        and config.get("User") == "10004:10004"
        and host.get("ReadonlyRootfs") is True
        and host.get("CapDrop") == ["ALL"]
        and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        and host.get("Memory") == 268435456
        and host.get("NanoCpus") == 1000000000
        and host.get("PidsLimit") == 64
        and host.get("NetworkMode") == network_id
        and set(observed_mounts) == set(secret_destinations)
        and all(mount.get("RW") is False for mount in observed_mounts.values())
    ):
        pytest.fail(
            "Notifications publisher container did not match its owned runtime profile"
        )
    if secret_destinations:
        publisher_environment = {
            value
            for value in (config.get("Env") or ())
            if value.startswith("NOTIFICATION_PUBLISH_")
        }
        if publisher_environment != {
            f"NOTIFICATION_PUBLISH_DATABASE_URL_FILE={secret_destinations[0]}",
            f"NOTIFICATION_PUBLISH_REDIS_URL_FILE={secret_destinations[1]}",
        }:
            pytest.fail("Notifications publisher received unexpected runtime settings")
    elif any(
        value.startswith("NOTIFICATION_PUBLISH_") for value in (config.get("Env") or ())
    ):
        pytest.fail("Notifications listener received publisher credentials")

    state = observed.get("State", {})
    if phase == "created":
        if not (
            state.get("Status") == "created"
            and state.get("Running") is False
            and state.get("Pid") == 0
            and (
                not networks
                or (
                    set(networks) == {network_name}
                    and networks[network_name].get("NetworkID") in (None, "")
                    and networks[network_name].get("EndpointID") in (None, "")
                )
            )
        ):
            pytest.fail(
                "Notifications container did not remain in verified created state"
            )
    elif phase == "running" or (
        phase == "started" and state.get("Status") == "running"
    ):
        if not (
            state.get("Status") == "running"
            and state.get("Running") is True
            and state.get("Pid", 0) > 0
            and set(networks) == {network_name}
            and networks[network_name].get("NetworkID") == network_id
            and networks[network_name].get("EndpointID")
            and all(
                endpoint.get("NetworkID") == network_id
                for endpoint in networks.values()
            )
        ):
            pytest.fail(
                "Notifications runtime endpoint did not match its private network"
            )
    elif phase in {"started", "stopped"}:
        if not (
            state.get("Status") == "exited"
            and state.get("Running") is False
            and state.get("Pid") == 0
            and set(networks) == {network_name}
            and networks[network_name].get("NetworkID") == network_id
            and networks[network_name].get("EndpointID") == ""
            and all(
                endpoint.get("NetworkID") == network_id
                for endpoint in networks.values()
            )
        ):
            pytest.fail("Exited Notifications runtime identity did not match")
    else:
        raise ValueError("Unsupported Notifications container lifecycle phase")
    return observed


def create_private_network(run_id: str) -> tuple[str, str]:
    name = f"aap-notify-live-{run_id[:12]}"
    try:
        _docker(
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--label",
            f"org.aap.acceptance={_CONTAINER_ACCEPTANCE}",
            "--label",
            f"org.aap.acceptance-id={run_id}",
            name,
        )
        network = inspect_docker("network", name)
        labels = network.get("Labels") or {}
        if not (
            network.get("Name") == name
            and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
            and labels.get("org.aap.acceptance-id") == run_id
            and network.get("Internal") is True
            and network.get("Id")
        ):
            pytest.fail("Notifications private network identity did not match")
        return name, network["Id"]
    except BaseException:
        _cleanup_partial_named_object("network", name, run_id)
        raise


def _named_object_exists(kind: str, name: str) -> bool:
    if kind == "network":
        arguments = ("network", "ls", "--format", "{{.Name}}")
    elif kind == "container":
        arguments = ("ps", "--all", "--format", "{{.Names}}")
    else:
        raise ValueError("Unsupported partial Notifications resource kind")
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("Could not inspect a partially created Notifications resource")
    return name in result.stdout.splitlines()


def _cleanup_partial_named_object(
    kind: str,
    name: str,
    run_id: str,
    *,
    network_name: str | None = None,
    network_id: str | None = None,
    image_id: str | None = None,
    test_case: str | None = None,
    secret_destinations: tuple[str, ...] = (),
) -> None:
    if not _named_object_exists(kind, name):
        return

    observed = inspect_docker(kind, name)
    labels = (
        observed.get("Config", {}).get("Labels")
        if kind == "container"
        else observed.get("Labels")
    )
    labels = labels or {}
    expected_name = f"/{name}" if kind == "container" else name
    if not (
        observed.get("Name") == expected_name
        and observed.get("Id")
        and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
        and labels.get("org.aap.acceptance-id") == run_id
    ):
        pytest.fail(
            "Refusing to clean a partially created foreign Notifications resource"
        )
    if kind == "container":
        if not network_name or not network_id or not image_id or not test_case:
            pytest.fail("Partial publisher container has incomplete expected identity")
        verify_live_container(
            observed["Id"],
            run_id=run_id,
            image_id=image_id,
            network_name=network_name,
            network_id=network_id,
            phase="created",
            container_name=name,
            test_case=test_case,
            secret_destinations=secret_destinations,
        )
        remove_owned_container(
            observed["Id"],
            run_id=run_id,
            network_name=network_name,
            network_id=network_id,
        )
        return
    if observed.get("Containers"):
        pytest.fail("Refusing to remove an attached partial Notifications network")
    _docker("network", "rm", observed["Id"])


def connect_owned_dependency(
    network_name: str,
    network_id: str,
    container_id: str,
    alias: str,
) -> None:
    _docker(
        "network",
        "connect",
        "--alias",
        alias,
        network_id,
        container_id,
    )
    network = inspect_docker("network", network_id)
    container = inspect_docker("container", container_id)
    endpoint = (
        container.get("NetworkSettings", {}).get("Networks", {}).get(network_name) or {}
    )
    if not (
        network.get("Id") == network_id
        and network.get("Name") == network_name
        and endpoint.get("NetworkID") == network_id
        and endpoint.get("EndpointID")
        and alias in (endpoint.get("Aliases") or [])
    ):
        pytest.fail("Owned dependency was not attached to the private network")


def disconnect_owned_dependency(
    network_name: str,
    network_id: str,
    container_id: str,
) -> None:
    container = inspect_docker("container", container_id)
    endpoint = (
        container.get("NetworkSettings", {}).get("Networks", {}).get(network_name) or {}
    )
    if endpoint:
        if endpoint.get("NetworkID") != network_id:
            pytest.fail("Refusing to detach an unexpected dependency network")
        if not endpoint.get("EndpointID"):
            pytest.fail("Refusing to detach a dependency without a live endpoint")
        _docker("network", "disconnect", network_id, container_id)
        after = inspect_docker("container", container_id)
        if network_name in (after.get("NetworkSettings", {}).get("Networks") or {}):
            pytest.fail("Owned dependency stayed attached to the private network")


def remove_owned_network(network_name: str, network_id: str, run_id: str) -> None:
    network = inspect_docker("network", network_id)
    labels = network.get("Labels") or {}
    if not (
        network.get("Id") == network_id
        and network.get("Name") == network_name
        and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
        and labels.get("org.aap.acceptance-id") == run_id
    ):
        pytest.fail("Refusing to remove a network outside this acceptance")
    if network.get("Containers"):
        pytest.fail("Refusing to remove a network with attached endpoints")
    _docker("network", "rm", network_id)


def prepare_secret_files(
    directory: Path,
    *,
    label: str,
    database_url: str,
    redis_url: str,
) -> tuple[Path, Path]:
    if re.fullmatch(r"[a-z0-9-]{1,32}", label) is None:
        raise ValueError("Invalid Notifications secret fixture label")
    secret_directory = directory / f"notifications-publisher-secrets-{label}"
    secret_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if secret_directory.is_symlink():
        pytest.fail("Notifications secret directory must not be a symlink")
    secret_directory.chmod(0o700)
    database_path = secret_directory / "database-url"
    redis_path = secret_directory / "redis-url"
    database_path.write_text(database_url, encoding="utf-8")
    redis_path.write_text(redis_url, encoding="utf-8")
    database_path.chmod(0o444)
    redis_path.chmod(0o444)
    return database_path, redis_path


def remove_secret_files(root: Path, secret_directory: Path) -> None:
    root = root.resolve()
    if (
        secret_directory.is_symlink()
        or secret_directory.parent.resolve() != root
        or not secret_directory.name.startswith("notifications-publisher-secrets-")
    ):
        pytest.fail("Refusing to remove an unexpected Notifications secret directory")
    if not secret_directory.exists():
        return
    if not secret_directory.is_dir():
        pytest.fail("Notifications secret fixture is not a directory")
    for path in secret_directory.iterdir():
        if (
            path.name not in {"database-url", "redis-url"}
            or path.is_symlink()
            or not path.is_file()
        ):
            pytest.fail("Refusing to remove unexpected Notifications secret content")
        path.unlink()
    secret_directory.rmdir()


def create_live_container(
    *,
    image_id: str,
    run_id: str,
    network_name: str,
    network_id: str,
    case: str,
    database_secret: Path | None = None,
    redis_secret: Path | None = None,
    network_alias: str | None = None,
    entrypoint: str | None = None,
    command: tuple[str, ...] | None = None,
) -> str:
    if (database_secret is None) != (redis_secret is None):
        raise ValueError("Both publisher secret files must be supplied together")
    name = f"aap-notify-{run_id[:10]}-{case}"
    arguments = [
        "create",
        "--name",
        name,
        "--network",
        network_id,
        "--label",
        f"org.aap.acceptance={_CONTAINER_ACCEPTANCE}",
        "--label",
        f"org.aap.acceptance-id={run_id}",
        "--label",
        f"org.aap.test-case={case}",
        "--user",
        "10004:10004",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
    ]
    if network_alias:
        arguments.extend(("--network-alias", network_alias))
    destinations: tuple[str, ...] = ()
    if database_secret is not None and redis_secret is not None:
        arguments.extend(
            (
                "--mount",
                f"type=bind,source={database_secret},target=/run/secrets/database-url,readonly",
                "--mount",
                f"type=bind,source={redis_secret},target=/run/secrets/redis-url,readonly",
                "--env",
                "NOTIFICATION_PUBLISH_DATABASE_URL_FILE=/run/secrets/database-url",
                "--env",
                "NOTIFICATION_PUBLISH_REDIS_URL_FILE=/run/secrets/redis-url",
            )
        )
        destinations = (
            "/run/secrets/database-url",
            "/run/secrets/redis-url",
        )
    process_command = notifications_container_command(
        entrypoint=entrypoint,
        command=command,
    )
    if entrypoint is not None:
        arguments.extend(("--entrypoint", entrypoint))
    arguments.extend((image_id, *process_command))
    try:
        container_id = _docker(*arguments, timeout=30)
        verify_live_container(
            container_id,
            run_id=run_id,
            image_id=image_id,
            network_name=network_name,
            network_id=network_id,
            phase="created",
            container_name=name,
            test_case=case,
            secret_destinations=destinations,
        )
        return container_id
    except BaseException:
        _cleanup_partial_named_object(
            "container",
            name,
            run_id,
            network_name=network_name,
            network_id=network_id,
            image_id=image_id,
            test_case=case,
            secret_destinations=destinations,
        )
        raise


def run_once_container(
    container_id: str,
    *,
    run_id: str,
    image_id: str,
    network_name: str,
    network_id: str,
    container_name: str,
    test_case: str,
    secrets_to_hide: tuple[str, ...],
) -> dict[str, int]:
    verify_live_container(
        container_id,
        run_id=run_id,
        image_id=image_id,
        network_name=network_name,
        network_id=network_id,
        phase="created",
        container_name=container_name,
        test_case=test_case,
        secret_destinations=(
            "/run/secrets/database-url",
            "/run/secrets/redis-url",
        ),
    )
    _docker("start", container_id)
    verify_live_container(
        container_id,
        run_id=run_id,
        image_id=image_id,
        network_name=network_name,
        network_id=network_id,
        phase="started",
        container_name=container_name,
        test_case=test_case,
        secret_destinations=(
            "/run/secrets/database-url",
            "/run/secrets/redis-url",
        ),
    )
    exit_code = _docker("wait", container_id, timeout=45)
    observed = verify_live_container(
        container_id,
        run_id=run_id,
        image_id=image_id,
        network_name=network_name,
        network_id=network_id,
        phase="stopped",
        container_name=container_name,
        test_case=test_case,
        secret_destinations=(
            "/run/secrets/database-url",
            "/run/secrets/redis-url",
        ),
    )
    if exit_code != "0" or observed.get("State", {}).get("Running") is not False:
        pytest.fail("Standalone Notifications publisher one-shot failed")
    stdout, stderr = container_logs(container_id)
    if any(secret and secret in stdout + stderr for secret in secrets_to_hide):
        pytest.fail("Notifications publisher output disclosed a secret")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        pytest.fail("Standalone Notifications publisher returned no result record")
    expected = {"claimed", "published", "retry", "stale", "unavailable"}
    if (
        not isinstance(result, dict)
        or set(result) != expected
        or any(type(value) is not int or value < 0 for value in result.values())
    ):
        pytest.fail("Standalone Notifications publisher returned an invalid result")
    return result


def container_logs(container_id: str) -> tuple[str, str]:
    """Capture publisher stdout and stderr separately without echoing either."""
    result = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("Could not inspect the owned Notifications container logs")
    return result.stdout, result.stderr


def wait_for_container_log(
    container_id: str,
    *,
    run_id: str,
    network_name: str,
    network_id: str,
    marker: str,
    timeout: float = 8,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = inspect_docker("container", container_id)
        labels = observed.get("Config", {}).get("Labels") or {}
        networks = observed.get("NetworkSettings", {}).get("Networks", {})
        if not (
            observed.get("Id") == container_id
            and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
            and labels.get("org.aap.acceptance-id") == run_id
            and observed.get("State", {}).get("Running") is True
            and set(networks) == {network_name}
            and networks[network_name].get("NetworkID") == network_id
            and networks[network_name].get("EndpointID")
            and all(
                endpoint.get("NetworkID") == network_id
                for endpoint in networks.values()
            )
        ):
            pytest.fail("Notifications listener container stopped unexpectedly")
        if marker in _docker("logs", container_id, timeout=10):
            return
        time.sleep(0.1)
    pytest.fail("Notifications private listener did not become ready")


def remove_owned_container(
    container_id: str,
    *,
    run_id: str,
    network_name: str,
    network_id: str,
) -> None:
    observed = inspect_docker("container", container_id)
    labels = observed.get("Config", {}).get("Labels") or {}
    container_name = f"aap-notify-{run_id[:10]}-{labels.get('org.aap.test-case', '')}"
    host = observed.get("HostConfig", {})
    state = observed.get("State", {})
    networks = observed.get("NetworkSettings", {}).get("Networks", {})
    if not (
        observed.get("Id") == container_id
        and observed.get("Name") == f"/{container_name}"
        and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
        and labels.get("org.aap.acceptance-id") == run_id
        and labels.get("org.aap.test-case")
        and host.get("NetworkMode") == network_id
    ):
        pytest.fail("Refusing to remove a container outside this acceptance")

    if state.get("Status") == "created":
        if not (
            state.get("Running") is False
            and state.get("Pid") == 0
            and (
                not networks
                or (
                    set(networks) == {network_name}
                    and networks[network_name].get("NetworkID") in (None, "")
                    and networks[network_name].get("EndpointID") in (None, "")
                )
            )
        ):
            pytest.fail("Refusing to remove an unverified created container")
        _docker("rm", container_id, timeout=15)
        return
    if state.get("Status") == "running":
        if not (
            state.get("Running") is True
            and state.get("Pid", 0) > 0
            and set(networks) == {network_name}
            and networks[network_name].get("NetworkID") == network_id
            and networks[network_name].get("EndpointID")
            and all(
                endpoint.get("NetworkID") == network_id
                for endpoint in networks.values()
            )
        ):
            pytest.fail("Refusing to stop a container outside the private network")
        _docker("stop", "--time", "2", container_id, timeout=15)
    stopped = inspect_docker("container", container_id)
    stopped_labels = stopped.get("Config", {}).get("Labels") or {}
    stopped_state = stopped.get("State", {})
    stopped_host = stopped.get("HostConfig", {})
    stopped_networks = stopped.get("NetworkSettings", {}).get("Networks", {})
    if not (
        stopped.get("Id") == container_id
        and stopped.get("Name") == f"/{container_name}"
        and stopped_labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
        and stopped_labels.get("org.aap.acceptance-id") == run_id
        and stopped_labels.get("org.aap.test-case") == labels.get("org.aap.test-case")
        and stopped_host.get("NetworkMode") == network_id
        and stopped_state.get("Status") == "exited"
        and stopped_state.get("Running") is False
        and stopped_state.get("Pid") == 0
        and set(stopped_networks) == {network_name}
        and stopped_networks[network_name].get("NetworkID") == network_id
        and stopped_networks[network_name].get("EndpointID") == ""
        and all(
            endpoint.get("NetworkID") == network_id
            for endpoint in stopped_networks.values()
        )
    ):
        pytest.fail("Refusing to remove a container whose ownership changed")
    _docker("rm", container_id, timeout=15)


def assert_no_owned_containers(run_id: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"label=org.aap.acceptance={_CONTAINER_ACCEPTANCE}",
            "--filter",
            f"label=org.aap.acceptance-id={run_id}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("Could not verify Notifications container cleanup")
    remaining = [value for value in result.stdout.splitlines() if value]
    for container_id in remaining:
        observed = inspect_docker("container", container_id)
        labels = observed.get("Config", {}).get("Labels") or {}
        if not (
            observed.get("Id") == container_id
            and labels.get("org.aap.acceptance") == _CONTAINER_ACCEPTANCE
            and labels.get("org.aap.acceptance-id") == run_id
        ):
            pytest.fail("Container cleanup query returned an unexpected identity")
    if remaining:
        pytest.fail("Notifications acceptance left an owned container behind")
