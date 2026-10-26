from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    parse_gpu_resources,
)
from app.config import Settings as APISettings


def _resource_json(*, mode: str | None = "observe") -> str:
    mode_field = f',"mode":"{mode}"' if mode is not None else ""
    return (
        '{"node-a/GPU-aaa":{"node_id":"node-a",'
        '"physical_device_token":"GPU-aaa","allocatable_mb":22000'
        f"{mode_field}}}}}"
    )


def _config(*, mode: str = "off", resources_json: str = "") -> GPUAuthoritySettings:
    resources, errors = parse_gpu_resources(resources_json)
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode(mode),
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=errors,
    )


def test_gpu_arbiter_defaults_to_safe_off() -> None:
    config = _config(mode="off", resources_json="{}")

    assert config.gpu_arbiter_mode is GPUArbiterMode.OFF
    assert config.gpu_arbiter_resources == {}
    assert config.gpu_arbiter_config_errors == ()
    assert config.gpu_arbiter_desired_mode("missing/index:0") is GPUArbiterMode.OFF
    assert config.gpu_arbiter_admission_timeout_seconds == 30
    assert config.gpu_arbiter_residency_cooldown_seconds == 30
    assert config.gpu_arbiter_rollout_enabled is False
    assert config.gpu_arbiter_collector_database_url_file == ""


def test_gpu_arbiter_rollout_release_gate_is_explicit() -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_rollout_enabled=True,
    )

    assert config.gpu_arbiter_rollout_enabled is True


def test_gpu_arbiter_accepts_bounded_admission_timeout() -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_admission_timeout_seconds=7,
    )

    assert config.gpu_arbiter_admission_timeout_seconds == 7


def test_gpu_arbiter_accepts_bounded_residency_cooldown() -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_residency_cooldown_seconds=7,
    )

    assert config.gpu_arbiter_residency_cooldown_seconds == 7


@pytest.mark.parametrize("timeout_seconds", (0, 3601))
def test_gpu_arbiter_rejects_out_of_range_admission_timeout(
    timeout_seconds: int,
) -> None:
    with pytest.raises(ValidationError):
        APISettings(
            _env_file=None,
            gpu_arbiter_admission_timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("cooldown_seconds", (0, 3601))
def test_gpu_arbiter_rejects_out_of_range_residency_cooldown(
    cooldown_seconds: int,
) -> None:
    with pytest.raises(ValidationError):
        APISettings(
            _env_file=None,
            gpu_arbiter_residency_cooldown_seconds=cooldown_seconds,
        )


def test_gpu_resources_parse_distinct_cards_and_resource_domains() -> None:
    resources, errors = parse_gpu_resources(
        "{"
        '"node-a/index:0":{"node_id":"node-a","physical_device_token":"index:0",'
        '"allocatable_mb":20000,"mode":"enforce"},'
        '"node-a/index:1":{"node_id":"node-a","physical_device_token":"index:1",'
        '"allocatable_mb":21000,"mode":"observe"},'
        '"node-b/index:0":{"node_id":"node-b","physical_device_token":"index:0",'
        '"allocatable_mb":22000,"mode":"enforce"}'
        "}"
    )
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.ENFORCE,
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=errors,
    )

    assert set(config.gpu_arbiter_resources) == {
        "node-a/index:0",
        "node-a/index:1",
        "node-b/index:0",
    }
    assert config.gpu_arbiter_desired_mode("node-a/index:0") is GPUArbiterMode.ENFORCE
    assert config.gpu_arbiter_desired_mode("node-a/index:1") is GPUArbiterMode.OBSERVE
    assert config.gpu_arbiter_desired_mode("node-b/index:0") is GPUArbiterMode.ENFORCE


def test_global_mode_is_a_ceiling_and_missing_resource_mode_stays_off() -> None:
    observed = _config(
        mode="observe",
        resources_json=_resource_json(mode="enforce"),
    )
    missing_mode = _config(
        mode="enforce",
        resources_json=_resource_json(mode=None),
    )

    assert observed.gpu_arbiter_desired_mode("node-a/GPU-aaa") is GPUArbiterMode.OBSERVE
    assert missing_mode.gpu_arbiter_desired_mode("node-a/GPU-aaa") is GPUArbiterMode.OFF


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"node-a/GPU-aaa":{"node_id":"node-a","physical_device_token":"GPU-aaa",'
        '"allocatable_mb":"22000"}}',
        '{"node-a/GPU-aaa":{"node_id":"node-a","physical_device_token":"GPU-aaa",'
        '"allocatable_mb":true}}',
        '{"wrong":{"node_id":"node-a","physical_device_token":"GPU-aaa",'
        '"allocatable_mb":22000}}',
        '{"node-a/GPU-aaa":{"node_id":"node-a","physical_device_token":"GPU-aaa",'
        '"allocatable_mb":22000,"unexpected":1}}',
        '{"node a/GPU-aaa":{"node_id":"node a","physical_device_token":"GPU-aaa",'
        '"allocatable_mb":22000}}',
        '{"node-a/cuda:0":{"node_id":"node-a","physical_device_token":"cuda:0",'
        '"allocatable_mb":22000}}',
        '{"node-a/index:-1":{"node_id":"node-a","physical_device_token":"index:-1",'
        '"allocatable_mb":22000}}',
    ],
)
def test_gpu_resources_report_invalid_or_weakly_typed_config(raw: str) -> None:
    resources, errors = parse_gpu_resources(raw)
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=errors,
    )

    assert config.gpu_arbiter_resources == {}
    assert config.gpu_arbiter_config_errors


def test_gpu_resources_reject_duplicate_json_keys() -> None:
    raw = (
        '{"node-a/GPU-aaa":{"node_id":"node-a","node_id":"node-b",'
        '"physical_device_token":"GPU-aaa","allocatable_mb":22000}}'
    )

    resources, errors = parse_gpu_resources(raw)
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=errors,
    )

    assert config.gpu_arbiter_resources == {}
    assert "重复 key" in config.gpu_arbiter_config_errors[0]


@pytest.mark.parametrize("global_mode", ["off", "observe", "enforce"])
def test_invalid_resource_json_stays_visible_and_has_no_desired_resource_mode(
    global_mode: str,
) -> None:
    config = _config(mode=global_mode, resources_json="{not-json")

    assert config.gpu_arbiter_resources == {}
    assert config.gpu_arbiter_config_errors
    assert config.gpu_arbiter_desired_mode("node-a/GPU-a") is GPUArbiterMode.OFF


def test_invalid_global_mode_is_rejected_instead_of_silently_downgraded() -> None:
    with pytest.raises(ValueError):
        GPUArbiterMode("enabled")


def test_blank_and_empty_object_resource_config_are_both_safe_empty_sets() -> None:
    blank = _config(resources_json="")
    empty_object = _config(resources_json="{}")

    assert blank.gpu_arbiter_resources == empty_object.gpu_arbiter_resources == {}
    assert (
        blank.gpu_arbiter_config_errors == empty_object.gpu_arbiter_config_errors == ()
    )


def test_one_invalid_resource_rejects_the_whole_configuration_snapshot() -> None:
    config = _config(
        resources_json=(
            '{"node-a/GPU-good":{"node_id":"node-a",'
            '"physical_device_token":"GPU-good","allocatable_mb":22000},'
            '"node-a/cuda:0":{"node_id":"node-a",'
            '"physical_device_token":"cuda:0","allocatable_mb":22000}}'
        ),
    )

    assert config.gpu_arbiter_resources == {}
    assert config.gpu_arbiter_config_errors
