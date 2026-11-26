from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    TrainArtifactV1,
    TrainReceiptV1,
    TrainerArchitectureV1,
    TrainerCapabilitiesV1,
    canonical_json_digest,
)

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.trainer_plugin import (
    QualifyTrainerPluginRequest,
    RegisterTrainerPluginRequest,
    TrainerPluginRecord,
)
from app.schemas.training_run import ExecutorClaim
from app.services import training_adapters as adapters
from app.services import training_runs as runs_service
from app.services.trainer_plugins import (
    entrypoint_digest,
    validate_qualification_report,
    validate_registration_payload,
)
from app.services.training_datasets import bind_training_authority
from tests.factory import create_project

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_0514 = REPO_ROOT / "apps" / "api" / "alembic" / "sql" / "0514_trainer_plugins.sql"
MIGRATION_0514 = (
    REPO_ROOT / "apps" / "api" / "alembic" / "versions" / "0514_trainer_plugins.py"
)
M2B_GRANTS = REPO_ROOT / "infra" / "docker" / "m2b-runtime-grants.sql"
MODULE_GRANTS = REPO_ROOT / "infra" / "docker" / "module-data-grants.sql"

SAMPLE_ARTIFACT = "ghcr.io/yoao-labs/trainer-dummy@sha256:" + "a" * 64
TOKEN = "test-executor-credential-" + "b" * 40
GPU_A = "GPU-aaaaaaaa-1111-2222-3333-444444444444"
GPU_B = "GPU-bbbbbbbb-1111-2222-3333-444444444444"


def _manifest(
    *,
    plugin_id: str = "yoao.trainer.dummy_cpu",
    framework_id: str = "dummy_cpu",
    entrypoint: list[str] | None = None,
    requires_gpu: bool = False,
    resource: dict | None = None,
    capabilities: list[str] | None = None,
    extra_top: dict | None = None,
    extra_ext: dict | None = None,
) -> dict:
    payload: dict = {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "trainer",
        "plugin_id": plugin_id,
        "version": "1.0.0",
        "display_name": "Dummy Trainer",
        "description": "Minimal deterministic trainer plugin.",
        "homepage": "https://example.com/trainer-dummy",
        "publisher": "YOAO Labs",
        "license": "Apache-2.0",
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "entrypoint": entrypoint or ["python3", "-m", "dummy_trainer"],
        "capabilities": capabilities or ["train", "evaluate", "export_weights"],
        "resource": {"memory_mb": 512} if resource is None else resource,
        "extension": {
            "framework_id": framework_id,
            "framework_version": "1.0.0",
            "requires_gpu": requires_gpu,
            **(extra_ext or {}),
        },
        **(extra_top or {}),
    }
    return payload


def _capabilities(
    *,
    framework_id: str = "dummy_cpu",
    base_asset_sha256: str | None = None,
    base_asset_filename: str | None = None,
    parameter_schema: dict | None = None,
) -> TrainerCapabilitiesV1:
    return TrainerCapabilitiesV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        framework_id=framework_id,
        framework_version="1.0.0",
        runtime_versions={"python": "3.12"},
        supported_tasks=["detect"],
        supported_dataset_contracts=["ultralytics-bbox-1"],
        architectures=[
            TrainerArchitectureV1(
                id="dummy_det_tiny",
                family="dummy",
                tasks=["detect"],
                base_asset_filename=base_asset_filename,
                base_asset_sha256=base_asset_sha256,
            )
        ],
        parameter_schema=(
            {
                "type": "object",
                "properties": {
                    "epochs": {
                        "type": "integer",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 20,
                    }
                },
            }
            if parameter_schema is None
            else parameter_schema
        ),
    )


def _record_from_payload(
    payload: dict, *, status: str = "registered"
) -> TrainerPluginRecord:
    now = datetime.now(timezone.utc)
    return TrainerPluginRecord(
        id=uuid.uuid4(),
        extension_point="trainer",
        status=status,
        revision=1,
        created_at=now,
        updated_at=now,
        **payload,
    )


def _qualify_body_for(
    record: TrainerPluginRecord, *, expected_revision: int = 1, **overrides
) -> dict:
    report: dict = {
        "passed": True,
        "plugin_id": record.plugin_id,
        "version": record.version,
        "execution_mode": record.execution_mode,
        "capabilities_digest": record.capabilities_digest,
    }
    if record.execution_mode == "container":
        report["artifact"] = record.artifact
    else:
        report["entrypoint_digest"] = entrypoint_digest(record.entrypoint)
    report.update(overrides)
    return {
        "expected_revision": expected_revision,
        "qualification_report_sha256": canonical_json_digest(report),
        "qualification_report": report,
    }


def test_migration_0514_sql_security_invariants():
    sql = SQL_0514.read_text(encoding="utf-8")
    migration = MIGRATION_0514.read_text(encoding="utf-8")
    m2b = M2B_GRANTS.read_text(encoding="utf-8")
    mod = MODULE_GRANTS.read_text(encoding="utf-8")

    assert 'revision = "bo0514"' in migration
    assert 'down_revision = "bo0513"' in migration
    assert "REVOKE ALL ON TABLE yoao_learning_v1.trainer_plugins FROM PUBLIC;" in sql
    assert "CREATE UNIQUE INDEX uq_trainer_plugins_active_framework" in sql
    assert "WHERE status <> 'retired'" in sql

    def _norm(value: str) -> str:
        collapsed = " ".join(value.split())
        return re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", collapsed))

    normalized_sql = _norm(sql)
    normalized_mod = _norm(mod)
    assert (
        "status IN ('qualified', 'enabled', 'disabled') AND qualification_report_sha256 IS NOT NULL"
        in normalized_sql
    )

    public_functions = (
        "yoao_learning_v1.list_trainer_plugins(boolean, text)",
        "yoao_learning_v1.get_trainer_plugin(text)",
        "yoao_learning_v1.get_enabled_trainer_plugin_by_framework(text)",
        "yoao_learning_v1.register_trainer_plugin(jsonb)",
        "yoao_learning_v1.qualify_trainer_plugin(jsonb)",
        "yoao_learning_v1.transition_trainer_plugin(jsonb)",
        "yoao_learning_v1.training_list_gpu_leases()",
    )
    private_helper = (
        "yoao_learning_v1._trainer_plugin_projection(yoao_learning_v1.trainer_plugins)"
    )

    for fn in (*public_functions, private_helper):
        assert f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC;" in normalized_sql
        assert f'ALTER FUNCTION {fn} OWNER TO :"learning_owner";' in normalized_mod

    assert (
        'ALTER TABLE yoao_learning_v1.trainer_plugins OWNER TO :"learning_owner";'
        in mod
    )
    assert sql.count("SET search_path = pg_catalog, pg_temp") >= 8
    assert sql.count("SECURITY DEFINER") >= 7

    normalized_m2b = _norm(m2b)
    for fn in public_functions:
        assert fn in normalized_m2b
        assert f"GRANT EXECUTE ON FUNCTION {fn} TO application_role;" in normalized_sql
    assert "_trainer_plugin_projection" not in normalized_m2b
    assert (
        "GRANT EXECUTE ON FUNCTION yoao_learning_v1._trainer_plugin_projection"
        not in normalized_sql
    )


def test_validate_registration_payload_accepts_container_and_local_process():
    caps = _capabilities(framework_id="dummy_gpu")
    container_req = RegisterTrainerPluginRequest(
        manifest=_manifest(
            plugin_id="yoao.trainer.dummy_gpu",
            framework_id="dummy_gpu",
            entrypoint=["python3", "/app/run.py", "-c", "/work/config.json"],
            requires_gpu=True,
            resource={"memory_mb": 4096},
        ),
        capabilities_snapshot=caps,
        execution_mode="container",
        source_tier="official",
        artifact=SAMPLE_ARTIFACT,
    )
    payload = validate_registration_payload(container_req)
    assert payload["plugin_id"] == "yoao.trainer.dummy_gpu"
    assert payload["framework_id"] == "dummy_gpu"
    assert payload["execution_mode"] == "container"
    assert payload["source_tier"] == "official"
    assert payload["artifact"] == SAMPLE_ARTIFACT
    assert payload["capabilities_digest"] == canonical_json_digest(caps)

    local_req = RegisterTrainerPluginRequest(
        manifest=_manifest(resource={}),
        capabilities_snapshot=_capabilities(),
        execution_mode="local_process",
        source_tier="third_party",
        artifact=None,
    )
    local_payload = validate_registration_payload(local_req)
    assert local_payload["artifact"] is None
    assert local_payload["execution_mode"] == "local_process"


@pytest.mark.parametrize(
    ("manifest_kwargs", "req_kwargs", "match"),
    [
        (
            {"extra_top": {"source_tier": "official"}},
            {"execution_mode": "local_process"},
            "source_tier",
        ),
        (
            {"extra_ext": {"source_tier": "official"}},
            {"execution_mode": "local_process"},
            "source_tier",
        ),
        (
            {"entrypoint": ["sh", "-c", "echo hi"]},
            {"execution_mode": "local_process"},
            "shell",
        ),
        (
            {"entrypoint": ["/bin/bash", "run.sh"]},
            {"execution_mode": "local_process"},
            "shell",
        ),
        (
            {"entrypoint": ["cmd.exe", "/c", "dir"]},
            {"execution_mode": "local_process"},
            "shell",
        ),
        (
            {"entrypoint": ["powershell", "-Command", "ls"]},
            {"execution_mode": "local_process"},
            "shell",
        ),
        (
            {"entrypoint": ["python3", "-c", "import os"]},
            {"execution_mode": "local_process"},
            "python -c",
        ),
        (
            {"entrypoint": ["python", "-uc", "1+1"]},
            {"execution_mode": "local_process"},
            "python -c",
        ),
        (
            {"entrypoint": ["python3", "-W", "ignore", "-c", "1"]},
            {"execution_mode": "local_process"},
            "python -c",
        ),
        (
            {"capabilities": ["train", "shell_exec"]},
            {"execution_mode": "local_process"},
            "capability",
        ),
        (
            {"capabilities": ["train", "train"]},
            {"execution_mode": "local_process"},
            "duplicate",
        ),
        (
            {"resource": {"memory_mb": 8}},
            {"execution_mode": "container", "artifact": SAMPLE_ARTIFACT},
            "memory_mb",
        ),
        (
            {"resource": {"memory_mb": 70000}},
            {"execution_mode": "container", "artifact": SAMPLE_ARTIFACT},
            "memory_mb",
        ),
        (
            {"resource": {}},
            {"execution_mode": "container", "artifact": SAMPLE_ARTIFACT},
            "memory_mb",
        ),
        (
            {},
            {"execution_mode": "container", "artifact": "ghcr.io/yoao/trainer:latest"},
            "sha256",
        ),
        (
            {"extra_top": {"artifact": "ghcr.io/yoao/other@sha256:" + "b" * 64}},
            {"execution_mode": "container", "artifact": SAMPLE_ARTIFACT},
            "conflicts",
        ),
        (
            {},
            {"execution_mode": "local_process", "artifact": SAMPLE_ARTIFACT},
            "forbids artifact",
        ),
        (
            {"extra_top": {"artifact": SAMPLE_ARTIFACT}},
            {"execution_mode": "local_process"},
            "forbids artifact",
        ),
        (
            {"framework_id": "other_fw"},
            {"execution_mode": "local_process"},
            "framework_id",
        ),
    ],
)
def test_validate_registration_payload_rejects_unsafe_inputs(
    manifest_kwargs, req_kwargs, match
):
    req = RegisterTrainerPluginRequest(
        manifest=_manifest(**manifest_kwargs),
        capabilities_snapshot=_capabilities(),
        **req_kwargs,
    )
    with pytest.raises(ValueError, match=match):
        validate_registration_payload(req)


def test_validate_qualification_report_enforces_digest_and_binding():
    container_payload = validate_registration_payload(
        RegisterTrainerPluginRequest(
            manifest=_manifest(),
            capabilities_snapshot=_capabilities(),
            execution_mode="container",
            artifact=SAMPLE_ARTIFACT,
        )
    )
    container_record = _record_from_payload(container_payload)
    valid_container = QualifyTrainerPluginRequest.model_validate(
        _qualify_body_for(container_record)
    )
    validate_qualification_report(container_record, valid_container)

    bad_digest = valid_container.model_copy(
        update={"qualification_report_sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="canonical digest"):
        validate_qualification_report(container_record, bad_digest)

    for bad_override in (
        {"passed": False},
        {"plugin_id": "yoao.trainer.other"},
        {"version": "2.0.0"},
        {"execution_mode": "local_process"},
        {"capabilities_digest": "f" * 64},
        {"artifact": "ghcr.io/yoao/wrong@sha256:" + "c" * 64},
    ):
        req = QualifyTrainerPluginRequest.model_validate(
            _qualify_body_for(container_record, **bad_override)
        )
        with pytest.raises(ValueError):
            validate_qualification_report(container_record, req)

    local_payload = validate_registration_payload(
        RegisterTrainerPluginRequest(
            manifest=_manifest(resource={}),
            capabilities_snapshot=_capabilities(),
            execution_mode="local_process",
            artifact=None,
        )
    )
    local_record = _record_from_payload(local_payload)
    validate_qualification_report(
        local_record,
        QualifyTrainerPluginRequest.model_validate(_qualify_body_for(local_record)),
    )
    bad_entrypoint = QualifyTrainerPluginRequest.model_validate(
        _qualify_body_for(local_record, entrypoint_digest="e" * 64)
    )
    with pytest.raises(ValueError, match="entrypoint_digest"):
        validate_qualification_report(local_record, bad_entrypoint)


def test_configured_resource_slots_and_selection(monkeypatch):
    monkeypatch.setattr(settings, "training_node_id", "node-1")
    monkeypatch.setattr(settings, "training_gpu_uuid", GPU_A)
    monkeypatch.setattr(
        settings, "training_gpu_uuids", f" {GPU_A} , {GPU_B} , invalid-id "
    )
    monkeypatch.setattr(settings, "training_cpu_slots", 2)

    slots = adapters.configured_resource_slots()
    assert [s["resource_id"] for s in slots] == [
        f"node-1/{GPU_A}",
        f"node-1/{GPU_B}",
        "node-1/cpu-0",
        "node-1/cpu-1",
    ]
    assert [s["device_kind"] for s in slots] == ["gpu", "gpu", "cpu", "cpu"]
    assert adapters.resource_id() == f"node-1/{GPU_A}"
    assert adapters.select_resource_slot(requires_gpu=True)["resource_id"] == (
        f"node-1/{GPU_A}"
    )
    assert adapters.select_resource_slot(requires_gpu=False)["resource_id"] == (
        "node-1/cpu-0"
    )

    monkeypatch.setattr(settings, "training_gpu_uuid", "")
    monkeypatch.setattr(settings, "training_gpu_uuids", "")
    assert adapters.resource_id() == "node-1/cpu-0"
    assert adapters.select_resource_slot(requires_gpu=True) is None


def test_runtime_compatible_adapter_revision_3():
    base_sha = "d" * 64
    container_cfg = {
        "adapter_revision": 3,
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "framework": "dummy_gpu",
        "framework_version": "1.0.0",
        "trainer_plugin": {
            "plugin_id": "yoao.trainer.dummy_gpu",
            "execution_mode": "container",
            "requires_gpu": True,
        },
        "base_model": {"id": "dummy_det_tiny", "sha256": base_sha},
    }
    good_gpu_runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["container"],
        "gpu_uuid": GPU_A,
        "base_asset_sha256": [base_sha],
    }
    assert runs_service.runtime_compatible(container_cfg, good_gpu_runtime) is True
    assert (
        runs_service.runtime_compatible(
            container_cfg, {**good_gpu_runtime, "executor_protocol": 2}
        )
        is False
    )
    assert (
        runs_service.runtime_compatible(
            container_cfg, {**good_gpu_runtime, "trainer_protocols": []}
        )
        is False
    )
    assert (
        runs_service.runtime_compatible(
            container_cfg, {**good_gpu_runtime, "execution_modes": ["local_process"]}
        )
        is False
    )
    assert (
        runs_service.runtime_compatible(
            container_cfg, {**good_gpu_runtime, "gpu_uuid": None}
        )
        is False
    )
    assert (
        runs_service.runtime_compatible(
            container_cfg, {**good_gpu_runtime, "base_asset_sha256": []}
        )
        is False
    )

    local_cfg = {
        "adapter_revision": 3,
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "framework": "dummy_cpu",
        "framework_version": "1.0.0",
        "trainer_plugin": {
            "plugin_id": "yoao.trainer.dummy_cpu",
            "execution_mode": "local_process",
            "requires_gpu": False,
        },
        "base_model": {"id": "dummy_det_tiny", "sha256": None},
    }
    local_runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["local_process"],
        "allowed_local_plugins": ["yoao.trainer.dummy_cpu"],
        "gpu_uuid": None,
    }
    assert runs_service.runtime_compatible(local_cfg, local_runtime) is True
    assert (
        runs_service.runtime_compatible(
            local_cfg, {**local_runtime, "allowed_local_plugins": []}
        )
        is False
    )


def test_verify_trainer_receipt_artifact(monkeypatch):
    run_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    prefix = f"projects/{uuid.uuid4()}/runs/{run_id}/"
    run = SimpleNamespace(id=run_id, project_id=uuid.uuid4())
    attempt = SimpleNamespace(id=attempt_id, run_id=run_id)
    monkeypatch.setattr(
        runs_service,
        "attempt_storage_owner",
        lambda _run, _attempt, _key=None: SimpleNamespace(
            bucket="training", prefix=prefix
        ),
    )

    weights_bytes = b"dummy-weights"
    weights_sha = hashlib.sha256(weights_bytes).hexdigest()

    def build_transport(receipt_payload: dict):
        body = json.dumps(receipt_payload).encode("utf-8")
        receipt_sha = hashlib.sha256(body).hexdigest()
        receipt_key = f"{prefix}attempts/{attempt_id}/result.json"

        class FakeS3Client:
            def get_object(self, *, Bucket, Key):
                assert Bucket == "training"
                assert Key == receipt_key
                return {
                    "ContentLength": len(body),
                    "Metadata": {"attempt-id": str(attempt_id)},
                    "Body": io.BytesIO(body),
                }

        class FakeTransport:
            client = FakeS3Client()

        artifacts = {
            "result.json": {
                "role": "config",
                "key": receipt_key,
                "sha256": receipt_sha,
                "size_bytes": len(body),
            },
            "best.pt": {
                "role": "weights",
                "key": f"{prefix}attempts/{attempt_id}/best.pt",
                "sha256": weights_sha,
                "size_bytes": len(weights_bytes),
            },
        }
        return FakeTransport(), artifacts

    good_receipt = TrainReceiptV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        status="succeeded",
        artifacts=[
            TrainArtifactV1(
                role="weights",
                filename="best.pt",
                sha256=weights_sha,
                size_bytes=len(weights_bytes),
            ),
            TrainArtifactV1(
                role="input_membership",
                filename="input-membership.json",
                sha256="1" * 64,
                size_bytes=42,
            ),
        ],
    ).model_dump(mode="json")
    transport, artifacts = build_transport(good_receipt)
    runs_service.verify_trainer_receipt_artifact(
        run, attempt, artifacts, transport=transport
    )

    with pytest.raises(ValueError, match="result.json"):
        runs_service.verify_trainer_receipt_artifact(
            run, attempt, {"best.pt": artifacts["best.pt"]}, transport=transport
        )

    failed_receipt = {
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "status": "failed",
        "artifacts": [],
        "failure_code": "native_exception",
        "failure_message": "boom",
    }
    bad_transport, bad_artifacts = build_transport(failed_receipt)
    with pytest.raises(ValueError, match="succeeded"):
        runs_service.verify_trainer_receipt_artifact(
            run, attempt, bad_artifacts, transport=bad_transport
        )

    tampered_sha = {
        **good_receipt,
        "artifacts": [
            {
                "role": "weights",
                "filename": "best.pt",
                "sha256": "0" * 64,
                "size_bytes": len(weights_bytes),
            }
        ],
    }
    t2, a2 = build_transport(tampered_sha)
    with pytest.raises(ValueError, match="mismatch"):
        runs_service.verify_trainer_receipt_artifact(run, attempt, a2, transport=t2)

    tampered_size = {
        **good_receipt,
        "artifacts": [
            {
                "role": "weights",
                "filename": "best.pt",
                "sha256": weights_sha,
                "size_bytes": len(weights_bytes) + 1,
            }
        ],
    }
    t3, a3 = build_transport(tampered_size)
    with pytest.raises(ValueError, match="mismatch"):
        runs_service.verify_trainer_receipt_artifact(run, attempt, a3, transport=t3)


async def test_admin_trainer_plugins_lifecycle_rbac_audit_and_sql_invariants(
    db_session, super_admin, project_admin, httpx_client
):
    _, admin_token = super_admin
    _, non_admin_token = project_admin
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    non_admin_headers = {"Authorization": f"Bearer {non_admin_token}"}

    proc_rows = (
        await db_session.execute(
            text(
                "SELECT p.proname, p.prosecdef, p.proconfig "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'yoao_learning_v1' "
                "AND p.proname IN ("
                "'list_trainer_plugins',"
                "'get_trainer_plugin',"
                "'get_enabled_trainer_plugin_by_framework',"
                "'register_trainer_plugin',"
                "'qualify_trainer_plugin',"
                "'transition_trainer_plugin',"
                "'training_list_gpu_leases',"
                "'_trainer_plugin_projection'"
                ")"
            )
        )
    ).all()
    procs = {
        row.proname: (row.prosecdef, list(row.proconfig or [])) for row in proc_rows
    }
    assert len(procs) == 8
    for name, (secdef, cfg) in procs.items():
        assert "search_path=pg_catalog, pg_temp" in cfg
        if name != "_trainer_plugin_projection":
            assert secdef is True

    forbidden = await httpx_client.get(
        "/api/v1/admin/trainer-plugins", headers=non_admin_headers
    )
    assert forbidden.status_code == 403

    plugin_id = f"yoao.trainer.test_{uuid.uuid4().hex[:8]}"
    framework_id = f"fw_{uuid.uuid4().hex[:8]}"
    register_body = {
        "manifest": _manifest(plugin_id=plugin_id, framework_id=framework_id),
        "capabilities_snapshot": _capabilities(framework_id=framework_id).model_dump(
            mode="json"
        ),
        "execution_mode": "local_process",
        "source_tier": "third_party",
        "artifact": None,
    }
    created = await httpx_client.post(
        "/api/v1/admin/trainer-plugins", headers=admin_headers, json=register_body
    )
    assert created.status_code == 201, created.text
    record = TrainerPluginRecord.model_validate(created.json())
    assert record.status == "registered"
    assert record.revision == 1
    assert record.extension_point == "trainer"

    dup_framework = await httpx_client.post(
        "/api/v1/admin/trainer-plugins",
        headers=admin_headers,
        json={
            **register_body,
            "manifest": _manifest(
                plugin_id=plugin_id + "_dup", framework_id=framework_id
            ),
        },
    )
    assert dup_framework.status_code == 409

    early_enable = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/enable",
        headers=admin_headers,
        json={"expected_revision": 1},
    )
    assert early_enable.status_code == 422

    stale_qualify = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/qualify",
        headers=admin_headers,
        json=_qualify_body_for(record, expected_revision=99),
    )
    assert stale_qualify.status_code == 409

    qualified = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/qualify",
        headers=admin_headers,
        json=_qualify_body_for(record, expected_revision=1),
    )
    assert qualified.status_code == 200, qualified.text
    assert qualified.json()["status"] == "qualified"
    assert qualified.json()["revision"] == 2

    enabled = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/enable",
        headers=admin_headers,
        json={"expected_revision": 2},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "enabled"
    assert enabled.json()["revision"] == 3

    qualify_while_enabled = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/qualify",
        headers=admin_headers,
        json=_qualify_body_for(record, expected_revision=3),
    )
    assert qualify_while_enabled.status_code == 422

    disabled = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/disable",
        headers=admin_headers,
        json={"expected_revision": 3},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["revision"] == 4

    retired = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/retire",
        headers=admin_headers,
        json={"expected_revision": 4},
    )
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    assert retired.json()["revision"] == 5

    after_retire = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/enable",
        headers=admin_headers,
        json={"expected_revision": 5},
    )
    assert after_retire.status_code == 422

    active_list = await httpx_client.get(
        "/api/v1/admin/trainer-plugins", headers=admin_headers
    )
    assert plugin_id not in {item["plugin_id"] for item in active_list.json()}
    all_list = await httpx_client.get(
        "/api/v1/admin/trainer-plugins",
        params={"include_retired": "true"},
        headers=admin_headers,
    )
    assert plugin_id in {item["plugin_id"] for item in all_list.json()}

    replacement = await httpx_client.post(
        "/api/v1/admin/trainer-plugins",
        headers=admin_headers,
        json={
            **register_body,
            "manifest": _manifest(
                plugin_id=plugin_id + "_v2", framework_id=framework_id
            ),
        },
    )
    assert replacement.status_code == 201, replacement.text

    audit_actions = list(
        (
            await db_session.scalars(
                text(
                    "SELECT action FROM audit_logs "
                    "WHERE target_type = 'trainer_plugin' AND target_id = :target_id "
                    "ORDER BY id ASC"
                ),
                {"target_id": plugin_id},
            )
        ).all()
    )
    assert audit_actions == [
        "trainer_plugin.register",
        "trainer_plugin.qualify",
        "trainer_plugin.enable",
        "trainer_plugin.disable",
        "trainer_plugin.retire",
    ]


async def test_enabled_cpu_plugin_catalog_preflight_and_claim(
    db_session, super_admin, httpx_client, monkeypatch
):
    from tests.training_consumer_fixtures import mock_legacy_transport

    mock_legacy_transport(monkeypatch)
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", TOKEN)
    monkeypatch.setattr(settings, "training_node_id", "test")
    monkeypatch.setattr(settings, "training_gpu_uuid", "")
    monkeypatch.setattr(settings, "training_gpu_uuids", "")
    monkeypatch.setattr(settings, "training_cpu_slots", 1)
    monkeypatch.setattr(settings, "training_catalog_json", {})
    monkeypatch.setattr(
        runs_service,
        "internal_download",
        lambda key, **kwargs: "http://owned-storage/" + key,
    )

    owner, admin_token = super_admin
    headers = {"Authorization": f"Bearer {admin_token}"}
    plugin_id = f"yoao.trainer.cpu_{uuid.uuid4().hex[:8]}"
    framework_id = f"dummy_{uuid.uuid4().hex[:8]}"

    reg = await httpx_client.post(
        "/api/v1/admin/trainer-plugins",
        headers=headers,
        json={
            "manifest": _manifest(
                plugin_id=plugin_id, framework_id=framework_id, requires_gpu=False
            ),
            "capabilities_snapshot": _capabilities(
                framework_id=framework_id
            ).model_dump(mode="json"),
            "execution_mode": "local_process",
            "source_tier": "third_party",
            "artifact": None,
        },
    )
    assert reg.status_code == 201, reg.text
    record = TrainerPluginRecord.model_validate(reg.json())
    qual = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/qualify",
        headers=headers,
        json=_qualify_body_for(record, expected_revision=1),
    )
    assert qual.status_code == 200, qual.text
    en = await httpx_client.post(
        f"/api/v1/admin/trainer-plugins/{plugin_id}/enable",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert en.status_code == 200, en.text

    project = await create_project(db_session, owner_id=owner.id)
    dataset = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        name="Frozen CPU",
        snapshot={},
        status="pending",
        manifest={
            "schema": 1,
            "content_digest": "c" * 64,
            "validation_digest": "d" * 64,
            "classes": ["car"],
        },
        package_key="owned/dataset.zip",
        package_sha256="e" * 64,
    )
    db_session.add(dataset)
    await db_session.flush()
    from app.services.training_dataset_storage import allocate

    ref = await allocate(db_session, dataset)
    dataset.status = "ready"
    dataset.package_key = ref["prefix"] + "dataset.zip"
    await db_session.flush()

    hub_catalog = await httpx_client.get("/api/v1/training/catalog", headers=headers)
    assert hub_catalog.status_code == 200, hub_catalog.text
    hub_data = hub_catalog.json()
    assert hub_data["enabled"] is True
    assert hub_data["resource_id"] == "test/cpu-0"
    assert [slot["resource_id"] for slot in hub_data["resource_slots"]] == [
        "test/cpu-0"
    ]
    plugin_item = next(
        item for item in hub_data["items"] if item["framework"] == framework_id
    )
    assert plugin_item["plugin_id"] == plugin_id
    assert plugin_item["execution_mode"] == "local_process"
    assert plugin_item["requires_gpu"] is False

    param_schema = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-runs/parameter-schema",
        params={"framework": framework_id, "base_model": "dummy_det_tiny"},
        headers=headers,
    )
    assert param_schema.status_code == 200, param_schema.text
    assert param_schema.json()["adapter_revision"] == 3

    payload = {
        "name": "CPU plugin run",
        "dataset_id": str(dataset.id),
        "framework": framework_id,
        "base_model": "dummy_det_tiny",
        "request_id": str(uuid.uuid4()),
        "parameters": {"epochs": 2},
    }
    preflight = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs:preflight",
        headers=headers,
        json=payload,
    )
    assert preflight.status_code == 200, preflight.text
    frozen = preflight.json()["config"]
    assert frozen["adapter_revision"] == 3
    assert frozen["trainer_protocol"] == TRAINER_PROTOCOL_VERSION
    assert frozen["trainer_plugin"]["plugin_id"] == plugin_id
    assert frozen["resource_id"] == "test/cpu-0"
    assert frozen["dataset_contract"] == "ultralytics-bbox-1"

    created = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs",
        headers=headers,
        json={**payload, "preflight_digest": preflight.json()["config_digest"]},
    )
    assert created.status_code == 202, created.text

    authority = bind_training_authority(db_session)
    bad_gpu_on_cpu = ExecutorClaim(
        executor_id="cpu-worker-1",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        resource_id="test/cpu-0",
        runtime={
            "executor_protocol": 3,
            "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
            "execution_modes": ["local_process"],
            "allowed_local_plugins": [plugin_id],
            "gpu_uuid": GPU_A,
        },
    )
    with pytest.raises(Exception) as exc:
        await runs_service.claim(
            db_session, bad_gpu_on_cpu, training_authority=authority
        )
    assert getattr(exc.value, "status_code", None) == 409

    good_claim = ExecutorClaim(
        executor_id="cpu-worker-1",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        resource_id="test/cpu-0",
        runtime={
            "executor_protocol": 3,
            "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
            "execution_modes": ["local_process"],
            "allowed_local_plugins": [plugin_id],
            "gpu_uuid": None,
        },
    )
    assigned = await runs_service.claim(
        db_session, good_claim, training_authority=authority
    )
    assert assigned is not None
    assert assigned["config"]["adapter_revision"] == 3
    assert assigned["config"]["trainer_plugin"]["plugin_id"] == plugin_id

    second_claim = await runs_service.claim(
        db_session,
        ExecutorClaim(
            executor_id="cpu-worker-2",
            boot_id=uuid.uuid4(),
            claim_id=uuid.uuid4(),
            resource_id="test/cpu-0",
            runtime=good_claim.runtime,
        ),
        training_authority=authority,
    )
    assert second_claim is None
