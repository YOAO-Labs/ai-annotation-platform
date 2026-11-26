from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from training_contract.trainer_plugin import (
    TrainerArchitectureV1,
    TrainerCapabilitiesV1,
    canonical_json_digest,
)
from typer.testing import CliRunner

from yoao.cli.main import app
from yoao.trainer_plugins import TrainerPluginRecord
from .conftest import API


def _sample_manifest() -> dict:
    return {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "trainer",
        "plugin_id": "yoao.trainer.dummy_cpu",
        "version": "1.0.0",
        "display_name": "Dummy CPU Trainer",
        "description": "CPU smoke trainer plugin.",
        "protocol_version": "yoao-trainer/v1",
        "entrypoint": ["python3", "-m", "dummy_trainer"],
        "capabilities": ["train", "evaluate", "export_weights"],
        "resource": {"memory_mb": 512},
        "extension": {
            "framework_id": "dummy_cpu",
            "framework_version": "1.0.0",
            "requires_gpu": False,
        },
    }


def _sample_capabilities() -> TrainerCapabilitiesV1:
    return TrainerCapabilitiesV1(
        protocol_version="yoao-trainer/v1",
        framework_id="dummy_cpu",
        framework_version="1.0.0",
        runtime_versions={"python": "3.12"},
        supported_tasks=["detect"],
        supported_dataset_contracts=["ultralytics-bbox-1"],
        architectures=[
            TrainerArchitectureV1(
                id="dummy_det_tiny",
                family="dummy",
                tasks=["detect"],
            )
        ],
    )


def _sample_record(
    *, status: str = "registered", revision: int = 1, report_sha: str | None = None
) -> dict:
    caps = _sample_capabilities()
    return {
        "id": str(uuid4()),
        "plugin_id": "yoao.trainer.dummy_cpu",
        "version": "1.0.0",
        "extension_point": "trainer",
        "framework_id": "dummy_cpu",
        "display_name": "Dummy CPU Trainer",
        "description": "CPU smoke trainer plugin.",
        "homepage": None,
        "publisher": None,
        "license": None,
        "source_tier": "third_party",
        "execution_mode": "local_process",
        "status": status,
        "revision": revision,
        "protocol_version": "yoao-trainer/v1",
        "artifact": None,
        "entrypoint": ["python3", "-m", "dummy_trainer"],
        "resource": {"memory_mb": 512},
        "capabilities": ["train", "evaluate", "export_weights"],
        "manifest": _sample_manifest(),
        "capabilities_snapshot": caps.model_dump(mode="json"),
        "capabilities_digest": canonical_json_digest(caps),
        "qualification_report_sha256": report_sha,
        "qualification_report": {"passed": True} if report_sha else None,
        "qualified_at": "2026-11-26T00:00:00Z" if report_sha else None,
        "enabled_at": "2026-11-26T00:01:00Z" if status == "enabled" else None,
        "disabled_at": "2026-11-26T00:02:00Z" if status == "disabled" else None,
        "retired_at": "2026-11-26T00:03:00Z" if status == "retired" else None,
        "created_at": "2026-11-26T00:00:00Z",
        "updated_at": "2026-11-26T00:00:00Z",
    }


def test_trainer_plugins_sdk_lifecycle(client, respx_mock):
    registered = _sample_record(status="registered", revision=1)
    report = {"passed": True, "plugin_id": "yoao.trainer.dummy_cpu"}
    report_sha = canonical_json_digest(report)
    qualified = _sample_record(status="qualified", revision=2, report_sha=report_sha)
    enabled = _sample_record(status="enabled", revision=3, report_sha=report_sha)
    disabled = _sample_record(status="disabled", revision=4, report_sha=report_sha)
    retired = _sample_record(status="retired", revision=5, report_sha=report_sha)

    register_route = respx_mock.post(f"{API}/admin/trainer-plugins").mock(
        return_value=httpx.Response(201, json=registered)
    )
    list_route = respx_mock.get(f"{API}/admin/trainer-plugins").mock(
        return_value=httpx.Response(200, json=[registered])
    )
    get_route = respx_mock.get(
        f"{API}/admin/trainer-plugins/yoao.trainer.dummy_cpu"
    ).mock(return_value=httpx.Response(200, json=registered))
    qualify_route = respx_mock.post(
        f"{API}/admin/trainer-plugins/yoao.trainer.dummy_cpu/qualify"
    ).mock(return_value=httpx.Response(200, json=qualified))
    enable_route = respx_mock.post(
        f"{API}/admin/trainer-plugins/yoao.trainer.dummy_cpu/enable"
    ).mock(return_value=httpx.Response(200, json=enabled))
    disable_route = respx_mock.post(
        f"{API}/admin/trainer-plugins/yoao.trainer.dummy_cpu/disable"
    ).mock(return_value=httpx.Response(200, json=disabled))
    retire_route = respx_mock.post(
        f"{API}/admin/trainer-plugins/yoao.trainer.dummy_cpu/retire"
    ).mock(return_value=httpx.Response(200, json=retired))

    created = client.trainer_plugins.register(
        _sample_manifest(),
        _sample_capabilities(),
        execution_mode="local_process",
        source_tier="third_party",
        artifact=None,
    )
    assert created.plugin_id == "yoao.trainer.dummy_cpu"
    assert created.status == "registered"
    assert json.loads(register_route.calls.last.request.content)["execution_mode"] == (
        "local_process"
    )

    items = client.trainer_plugins.list(include_retired=True, status="registered")
    assert len(items) == 1
    assert list_route.calls.last.request.url.params["include_retired"] == "true"
    assert list_route.calls.last.request.url.params["status"] == "registered"

    fetched = client.trainer_plugins.get("yoao.trainer.dummy_cpu")
    assert fetched.plugin_id == "yoao.trainer.dummy_cpu"
    assert get_route.called

    after_qualify = client.trainer_plugins.qualify(
        "yoao.trainer.dummy_cpu",
        expected_revision=1,
        qualification_report_sha256=report_sha,
        qualification_report=report,
    )
    assert after_qualify.status == "qualified"
    assert (
        json.loads(qualify_route.calls.last.request.content)[
            "qualification_report_sha256"
        ]
        == report_sha
    )

    after_enable = client.trainer_plugins.enable(
        "yoao.trainer.dummy_cpu", expected_revision=2
    )
    assert after_enable.status == "enabled"
    assert json.loads(enable_route.calls.last.request.content) == {
        "expected_revision": 2
    }

    after_disable = client.trainer_plugins.disable(
        "yoao.trainer.dummy_cpu", expected_revision=3
    )
    assert after_disable.status == "disabled"
    assert json.loads(disable_route.calls.last.request.content) == {
        "expected_revision": 3
    }

    after_retire = client.trainer_plugins.retire(
        "yoao.trainer.dummy_cpu", expected_revision=4
    )
    assert after_retire.status == "retired"
    assert json.loads(retire_route.calls.last.request.content) == {
        "expected_revision": 4
    }


@pytest.mark.parametrize("command", ["enable", "disable", "retire"])
def test_trainer_cli_destructive_commands_require_yes(monkeypatch, command):
    def should_not_connect(*args, **kwargs):
        raise AssertionError("must not connect before confirmation")

    monkeypatch.setattr("yoao.cli.plugins.get_client", should_not_connect)
    result = CliRunner().invoke(
        app,
        [
            "plugins",
            "trainer",
            command,
            "yoao.trainer.dummy_cpu",
            "--expected-revision",
            "2",
            "--json",
        ],
    )
    assert result.exit_code != 0


def test_trainer_cli_full_lifecycle(monkeypatch, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    caps_path = tmp_path / "caps.json"
    report_path = tmp_path / "report.json"

    manifest = _sample_manifest()
    caps = _sample_capabilities().model_dump(mode="json")
    report = {"passed": True, "plugin_id": "yoao.trainer.dummy_cpu"}
    expected_sha = canonical_json_digest(report)

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    caps_path.write_text(json.dumps(caps), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    calls: list[tuple[str, dict]] = []

    class FakeTrainerPlugins:
        def list(self, *, include_retired=False, status=None):
            calls.append(
                ("list", {"include_retired": include_retired, "status": status})
            )
            return [TrainerPluginRecord.model_validate(_sample_record())]

        def get(self, plugin_id):
            calls.append(("get", {"plugin_id": plugin_id}))
            return TrainerPluginRecord.model_validate(_sample_record())

        def register(
            self,
            manifest_arg,
            caps_arg,
            *,
            execution_mode,
            source_tier,
            artifact,
        ):
            calls.append(
                (
                    "register",
                    {
                        "plugin_id": manifest_arg["plugin_id"],
                        "framework_id": caps_arg["framework_id"],
                        "execution_mode": execution_mode,
                        "source_tier": source_tier,
                        "artifact": artifact,
                    },
                )
            )
            return TrainerPluginRecord.model_validate(_sample_record())

        def qualify(
            self,
            plugin_id,
            *,
            expected_revision,
            qualification_report_sha256,
            qualification_report,
        ):
            calls.append(
                (
                    "qualify",
                    {
                        "plugin_id": plugin_id,
                        "expected_revision": expected_revision,
                        "sha256": qualification_report_sha256,
                    },
                )
            )
            return TrainerPluginRecord.model_validate(
                _sample_record(
                    status="qualified",
                    revision=expected_revision + 1,
                    report_sha=qualification_report_sha256,
                )
            )

        def enable(self, plugin_id, *, expected_revision):
            calls.append(
                ("enable", {"plugin_id": plugin_id, "revision": expected_revision})
            )
            return TrainerPluginRecord.model_validate(
                _sample_record(
                    status="enabled",
                    revision=expected_revision + 1,
                    report_sha=expected_sha,
                )
            )

        def disable(self, plugin_id, *, expected_revision):
            calls.append(
                ("disable", {"plugin_id": plugin_id, "revision": expected_revision})
            )
            return TrainerPluginRecord.model_validate(
                _sample_record(
                    status="disabled",
                    revision=expected_revision + 1,
                    report_sha=expected_sha,
                )
            )

        def retire(self, plugin_id, *, expected_revision):
            calls.append(
                ("retire", {"plugin_id": plugin_id, "revision": expected_revision})
            )
            return TrainerPluginRecord.model_validate(
                _sample_record(
                    status="retired",
                    revision=expected_revision + 1,
                    report_sha=expected_sha,
                )
            )

    class FakeClient:
        trainer_plugins = FakeTrainerPlugins()

    @contextmanager
    def fake_get_client(_json_output):
        yield FakeClient()

    monkeypatch.setattr("yoao.cli.plugins.get_client", fake_get_client)
    runner = CliRunner()

    res_reg = runner.invoke(
        app,
        [
            "plugins",
            "trainer",
            "register",
            "--manifest",
            str(manifest_path),
            "--capabilities",
            str(caps_path),
            "--execution-mode",
            "local_process",
            "--json",
        ],
    )
    assert res_reg.exit_code == 0, res_reg.output
    assert json.loads(res_reg.output)["status"] == "registered"

    res_list = runner.invoke(
        app, ["plugins", "trainer", "list", "--include-retired", "--json"]
    )
    assert res_list.exit_code == 0, res_list.output
    assert len(json.loads(res_list.output)) == 1

    res_get = runner.invoke(
        app, ["plugins", "trainer", "get", "yoao.trainer.dummy_cpu", "--json"]
    )
    assert res_get.exit_code == 0, res_get.output
    assert json.loads(res_get.output)["plugin_id"] == "yoao.trainer.dummy_cpu"

    res_qual = runner.invoke(
        app,
        [
            "plugins",
            "trainer",
            "qualify",
            "yoao.trainer.dummy_cpu",
            "--expected-revision",
            "1",
            "--report",
            str(report_path),
            "--json",
        ],
    )
    assert res_qual.exit_code == 0, res_qual.output
    assert json.loads(res_qual.output)["status"] == "qualified"
    assert calls[-1] == (
        "qualify",
        {
            "plugin_id": "yoao.trainer.dummy_cpu",
            "expected_revision": 1,
            "sha256": expected_sha,
        },
    )

    for cmd, rev, expected_status in (
        ("enable", "2", "enabled"),
        ("disable", "3", "disabled"),
        ("retire", "4", "retired"),
    ):
        res = runner.invoke(
            app,
            [
                "plugins",
                "trainer",
                cmd,
                "yoao.trainer.dummy_cpu",
                "--expected-revision",
                rev,
                "--yes",
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["status"] == expected_status
