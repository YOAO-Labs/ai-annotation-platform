"""Retire only the canonical Raster run's fresh, exclusive MinIO provider."""

from __future__ import annotations

from pathlib import Path
import re

from fixture_store import MINIO_IMAGE, _root_credentials
from storage_s3_contract import require
from verify_storage_candidates import Owner, command, host_port
from worktree_env import state_path


def _items(owner, resources):
    require(isinstance(owner, Owner), "raster_provider_owner_invalid")
    store = owner.data["raster"]
    journal = state_path(Path(resources["root"]), "e2e", "raster-fault-runs")
    require(
        owner.path.name == "owner.json"
        and owner.path.parent.parent == journal
        and re.fullmatch(r"[0-9a-f]{32}", owner.path.parent.name) is not None
        and owner.data["checkout"] == resources["root"],
        "raster_provider_journal_changed",
    )
    require(
        resources["mode"] == "e2e"
        and store["resources"] == resources
        and store["bucket"] == resources["buckets"]["MINIO_BUCKET"]
        and store["initial_bucket_count"] == 0
        and owner.data["cleaned"] is False,
        "raster_provider_scope_changed",
    )
    expected = {
        ("container", store["container_id"]),
        ("network", store["network_id"]),
        ("volume", store["volume_id"]),
    }
    actual = owner.data["resources"]
    require(
        len(actual) == 3
        and {(item["kind"], item["id"]) for item in actual} == expected,
        "raster_store_topology_changed",
    )
    return {item["kind"]: item for item in actual}


def _require_producer_boundary(owner):
    store = owner.data["raster"]
    images = store["images"]
    require(
        store["state"] == "consumers_stopped"
        and store.get("consumer_exit") == 0
        and "raster_shutdown" not in owner.data
        and images.get("state") == "stopped"
        and images.get("shutdown_requested") is True
        and images.get("exit_code") == 0
        and isinstance(images.get("database_role_oid"), int)
        and bool(store.get("pre_retirement_native_identities")),
        "raster_producer_boundary_unproven",
    )


def verify_exclusive_provider(owner, backend):
    """Prove this live endpoint, buckets, mount and network have one owner."""
    resources = backend.resources
    items = _items(owner, resources)
    store = owner.data["raster"]
    _require_producer_boundary(owner)
    require(
        backend.settings.minio_endpoint == store["endpoint"]
        and backend.settings.minio_use_ssl is False,
        "raster_provider_endpoint_changed",
    )
    access, secret = _root_credentials(owner)
    require(
        access == backend.settings.minio_access_key
        and secret == backend.settings.minio_secret_key,
        "raster_provider_credentials_changed",
    )
    container = owner.verified(items["container"])
    network = owner.verified(items["network"])
    volume = owner.verified(items["volume"])
    state = container.get("State") or {}
    config = container.get("Config") or {}
    mounts = container.get("Mounts") or []
    attached = (container.get("NetworkSettings") or {}).get("Networks") or {}
    birth = store.get("provider_birth") or {}
    require(
        config.get("Image") == MINIO_IMAGE
        and config.get("Cmd") == ["server", "/data", "--console-address", ":9001"]
        and state.get("Running") is True
        and state.get("Status") == "running"
        and isinstance(state.get("Pid"), int)
        and state["Pid"] > 0
        and bool(state.get("StartedAt"))
        and birth.get("container_id") == items["container"]["id"]
        and birth.get("pid") == state["Pid"]
        and birth.get("started_at") == state["StartedAt"]
        and container.get("RestartCount") == 0
        and len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == items["volume"]["id"]
        and mounts[0].get("Destination") == "/data"
        and mounts[0].get("RW") is True
        and volume.get("Driver") == "local"
        and network.get("Driver") == "bridge"
        and network.get("Name") == store["network_name"]
        and set(network.get("Containers") or {}) == {items["container"]["id"]}
        and set(attached) == {store["network_name"]}
        and store["endpoint"]
        == f"127.0.0.1:{host_port(owner, items['container'], 9000)}",
        "raster_store_physical_identity_changed",
    )
    environment = config.get("Env") or []
    require(
        [value for value in environment if value.startswith("MINIO_ROOT_USER=")]
        == ["MINIO_ROOT_USER=" + access]
        and [value for value in environment if value.startswith("MINIO_ROOT_PASSWORD=")]
        == ["MINIO_ROOT_PASSWORD=" + secret],
        "raster_provider_credentials_changed",
    )
    users = command(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            "volume=" + items["volume"]["id"],
            "--format",
            "{{.ID}}",
        ]
    ).splitlines()
    require(users == [items["container"]["id"]], "raster_provider_volume_shared")
    expected = set(resources["buckets"].values())
    entries = backend.s3.list_buckets().get("Buckets")
    require(
        isinstance(entries, list)
        and len(entries) == len(expected)
        and {item.get("Name") for item in entries} == expected,
        "raster_provider_bucket_inventory_changed",
    )
    for bucket in expected:
        info = backend.bucket_info(bucket)
        require(
            info is not None and info.get("owner") == resources["owner"],
            "raster_provider_bucket_owner_changed",
        )
    return {
        "started_at": state["StartedAt"],
        "pid": state["Pid"],
        "buckets": sorted(expected),
    }


def retire_provider(owner, backend):
    """Signal one live MinIO, observe its clean exit, and remove exact resources."""
    require(
        "provider_retirement" not in owner.data["raster"],
        "raster_provider_retirement_already_started",
    )
    snapshot = verify_exclusive_provider(owner, backend)
    items = _items(owner, backend.resources)
    container, volume, network = (
        items[name] for name in ("container", "volume", "network")
    )
    receipt = {
        "state": "signal_intent",
        "container_id": container["id"],
        "volume_id": volume["id"],
        "network_id": network["id"],
        **snapshot,
    }
    owner.data["raster"]["provider_retirement"] = receipt
    owner.save()
    # SIGTERM is a signal request, not docker stop's timed SIGKILL fallback.
    acknowledged = command(["docker", "kill", "--signal", "SIGTERM", container["id"]])
    require(
        acknowledged in {container["id"], container["name"]},
        "raster_provider_signal_unconfirmed",
    )
    receipt.update(
        state="signal_acknowledged", signal="SIGTERM", signal_ack=acknowledged
    )
    owner.save()
    exit_code = command(["docker", "wait", container["id"]])
    require(
        exit_code.isdecimal() and int(exit_code) <= 255, "raster_provider_wait_invalid"
    )
    receipt.update(state="wait_acknowledged", wait_exit=int(exit_code))
    owner.save()
    require(exit_code == "0", "raster_provider_exit_unsettled")
    observed = owner.verified(container)
    state = observed.get("State") or {}
    require(
        state.get("Status") == "exited"
        and state.get("Running") is False
        and state.get("ExitCode") == 0
        and state.get("Pid") == 0
        and state.get("OOMKilled") is False
        and not state.get("Error")
        and state.get("StartedAt") == snapshot["started_at"]
        and bool(state.get("FinishedAt"))
        and observed.get("RestartCount") == 0,
        "raster_provider_exit_unsettled",
    )
    receipt.update(
        state="exit_observed",
        wait_exit=0,
        finished_at=state["FinishedAt"],
        exit_state={
            "status": state["Status"],
            "running": state["Running"],
            "pid": state["Pid"],
            "exit_code": state["ExitCode"],
            "oom_killed": state["OOMKilled"],
            "error_empty": not state.get("Error"),
            "restart_count": observed["RestartCount"],
        },
    )
    owner.save()

    receipt["state"] = "container_remove_intent"
    owner.save()
    removed = command(["docker", "rm", container["id"]])
    require(
        removed in {container["id"], container["name"]},
        "raster_provider_remove_unconfirmed",
    )
    receipt.update(state="container_remove_acknowledged", container_removed=removed)
    owner.save()
    require(owner.inspect(container) is None, "raster_provider_container_still_present")
    container["state"] = "absent"
    receipt.update(state="container_removed", container_removed=removed)
    owner.save()

    owner.verified(volume)
    users = command(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            "volume=" + volume["id"],
            "--format",
            "{{.ID}}",
        ]
    )
    require(not users, "raster_provider_volume_shared")
    receipt["state"] = "volume_remove_intent"
    owner.save()
    removed = command(["docker", "volume", "rm", volume["id"]])
    require(removed == volume["id"], "raster_provider_volume_remove_unconfirmed")
    receipt.update(state="volume_remove_acknowledged", volume_removed=removed)
    owner.save()
    require(owner.inspect(volume) is None, "raster_provider_volume_still_present")
    volume["state"] = "absent"
    receipt.update(state="volume_removed", volume_removed=removed)
    owner.save()

    observed_network = owner.verified(network)
    require(not observed_network.get("Containers"), "raster_provider_network_in_use")
    receipt["state"] = "network_remove_intent"
    owner.save()
    removed = command(["docker", "network", "rm", network["id"]])
    require(
        removed in {network["id"], network["name"]},
        "raster_provider_network_remove_unconfirmed",
    )
    receipt.update(state="network_remove_acknowledged", network_removed=removed)
    owner.save()
    require(owner.inspect(network) is None, "raster_provider_network_still_present")
    network["state"] = "absent"
    receipt.update(state="retired", network_removed=removed)
    owner.save()


def require_retired_provider(owner, resources):
    """Require affirmative phase receipts plus exact removal, never absence alone."""
    items = _items(owner, resources)
    _require_producer_boundary(owner)
    receipt = owner.data["raster"].get("provider_retirement") or {}
    birth = owner.data["raster"].get("provider_birth") or {}
    require(
        receipt.get("state") == "retired"
        and receipt.get("container_id") == items["container"]["id"]
        and receipt.get("volume_id") == items["volume"]["id"]
        and receipt.get("network_id") == items["network"]["id"]
        and receipt.get("buckets") == sorted(resources["buckets"].values())
        and receipt.get("signal") == "SIGTERM"
        and receipt.get("signal_ack")
        in {items["container"]["id"], items["container"]["name"]}
        and receipt.get("wait_exit") == 0
        and bool(receipt.get("started_at"))
        and bool(receipt.get("finished_at"))
        and birth.get("container_id") == receipt.get("container_id")
        and type(birth.get("pid")) is int
        and birth["pid"] > 0
        and birth["pid"] == receipt.get("pid")
        and birth.get("started_at") == receipt.get("started_at")
        and receipt.get("exit_state")
        == {
            "status": "exited",
            "running": False,
            "pid": 0,
            "exit_code": 0,
            "oom_killed": False,
            "error_empty": True,
            "restart_count": 0,
        }
        and receipt.get("container_removed")
        in {items["container"]["id"], items["container"]["name"]}
        and receipt.get("volume_removed") == items["volume"]["id"]
        and receipt.get("network_removed")
        in {items["network"]["id"], items["network"]["name"]}
        and all(item.get("state") == "absent" for item in items.values()),
        "raster_provider_retirement_unproven",
    )
    for item in items.values():
        require(
            owner.inspect(item) is None, "raster_provider_retirement_identity_changed"
        )
