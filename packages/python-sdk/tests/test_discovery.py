"""Offline discovery behavior and packaged catalog drift checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from ai_annotation.discovery import describe_operation, list_capabilities

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
GENERATOR = PACKAGE_ROOT / "scripts" / "generate_operation_catalog.py"
SNAPSHOT = REPO_ROOT / "apps" / "api" / "openapi.snapshot.json"


def test_capabilities_are_bounded_filterable_and_cover_the_snapshot():
    page = list_capabilities(tag="training-runs", status="covered", limit=2)
    assert len(page["operations"]) == 2
    assert page["next_offset"] == 2
    assert all(item["status"] == "covered" for item in page["operations"])

    all_rows = []
    offset = 0
    while True:
        page = list_capabilities(limit=200, offset=offset)
        all_rows.extend(page["operations"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    counts = Counter(item["status"] for item in all_rows)
    assert Counter(page["coverage_counts"]) == counts


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "unknown"}, "unknown status"),
        ({"status": []}, "status must be a string"),
        ({"tag": "future-tag"}, "unknown tag"),
        ({"tag": []}, "tag must be a string"),
        ({"limit": 201}, "limit must"),
        ({"offset": -1}, "offset must"),
    ],
)
def test_capabilities_reject_unknown_filters_and_unbounded_pages(kwargs, message):
    with pytest.raises(ValueError, match=message):
        list_capabilities(**kwargs)


def test_describe_resolves_sdk_and_cli_aliases_and_expands_request_model():
    by_sdk = describe_operation("training_runs.create")
    by_cli = describe_operation("learning.runs.create")

    assert (
        by_sdk["name"]
        == by_cli["name"]
        == "POST /api/v1/projects/{project_id}/training-runs"
    )
    assert by_sdk["matched_sdk_method"] == "training_runs.create"
    assert by_cli["matched_cli_alias"] == "learning.runs.create"
    sdk = by_sdk["sdk"][0]
    assert sdk["return_type"] == "TrainingRunOut"
    assert [parameter["name"] for parameter in sdk["parameters"]] == [
        "project_id",
        "body",
    ]
    model = sdk["request_schemas"][0]
    assert model["model"] == "TrainingRunCreate"
    assert model["schema"]["properties"]["dataset_id"]["format"] == "uuid"
    assert "preflight_digest" in model["schema"]["properties"]
    assert by_sdk["request_body"]["required"] is True
    assert by_sdk["cli"][0]["path"] == "aap learning runs create"
    assert next(
        parameter
        for parameter in by_sdk["cli"][0]["parameters"]
        if parameter["name"] == "body_path"
    )["options"] == ["--body"]
    assert any("fresh digest" in item for item in by_sdk["constraints"])
    assert any("request_id" in item for item in by_sdk["constraints"])
    assert by_sdk["authority"].endswith("server remains authoritative.")

    export = describe_operation("exports.create")
    assert export["matched_sdk_method"] == "exports.create"


def test_describe_exposes_internal_request_dto_and_opaque_json_map():
    login = describe_operation("auth.login")["sdk"][0]
    assert "constructed_models" not in login
    assert len(login["request_schemas"]) == 1
    request = login["request_schemas"][0]
    assert request["model"] == "AuthLoginRequest" and request["parameter"] is None
    assert set(request["schema"]["required"]) == {"email", "password"}
    assert request["schema"]["additionalProperties"] is False
    assert login["response_schema"]["properties"]["access_token"]["type"] == "string"
    preferences = describe_operation("account.update_preferences")["sdk"][0]
    assert preferences["request_schemas"][0]["parameter"] == "patch"
    assert preferences["request_schemas"][0]["schema"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert preferences["response_schema"]["type"] == "object"
    user_list = describe_operation("users.list")["sdk"][0]
    assert user_list["response_schema"]["type"] == "array"
    assert "request_schemas" not in user_list


def test_all_packaged_sdk_methods_have_resolvable_descriptions():
    catalog = json.loads(
        (
            PACKAGE_ROOT / "src/ai_annotation/_operation_catalog.generated.json"
        ).read_text()
    )
    for method in catalog["sdk_methods"]:
        described = describe_operation(method["name"])
        assert described["name"]
        assert described.get("matched_sdk_method", described["name"]) == method["name"]


@pytest.mark.parametrize(
    "alias,method",
    [("accept", "accept_job"), ("discard", "discard_job"), ("cancel", "cancel_job")],
)
def test_tracker_cli_decisions_remain_discoverable(alias, method):
    operation = describe_operation(f"video-tracker.jobs.{alias}")
    assert f"video_tracker.{method}" in operation["sdk_methods"]
    assert operation["cli"][0]["confirmation"] is True


def test_video_review_description_preserves_version_and_mask_workflow():
    edit = describe_operation("video_review.save_mask_keyframe")
    assert any("If-Match" in item for item in edit["constraints"])
    assert "expected_version" in {item["name"] for item in edit["sdk"][0]["parameters"]}
    upload = describe_operation("video_review.upload_mask_content")
    schema = upload["sdk"][0]["request_schemas"][0]["schema"]
    assert schema["properties"]["counts"]["items"]["type"] == "integer"
    assert schema["properties"]["storage_encoding"]
    assert upload["cli"][0]["confirmation"] is True
    content_upload = describe_operation("files.upload_content")
    assert content_upload["kind"] == "sdk_method"
    assert content_upload["external_http"] is True
    assert content_upload["api_operations"] == []
    assert content_upload["cli"][0]["confirmation"] is True


def test_describe_includes_cursor_and_compare_and_set_rules():
    logs = describe_operation("training_runs.logs")
    assert any("both" in item for item in logs["constraints"])
    assert any("next_cursor" in item for item in logs["constraints"])

    disable = describe_operation("learning.policies.disable")
    assert any("expected_revision" in item for item in disable["constraints"])

    create_dataset = describe_operation("datasets.create")
    assert create_dataset["sdk"][0]["python_only"] is True
    assert "opaque" in create_dataset["sdk"][0]["invocation_note"]


def test_describe_aggregates_multi_route_sdk_methods_and_helpers():
    upload = describe_operation("datasets.upload_files")
    assert upload["kind"] == "sdk_method"
    assert {item["name"] for item in upload["api_operations"]} == {
        "POST /api/v1/datasets/{dataset_id}/items/upload-init",
        "POST /api/v1/datasets/{dataset_id}/items/upload-complete/{item_id}",
    }

    wait = describe_operation("jobs.wait")
    assert wait["kind"] == "sdk_method"
    assert [item["name"] for item in wait["api_operations"]] == [
        "GET /api/v1/async-jobs/{job_id}"
    ]
    assert any(command["alias"] == "jobs.wait" for command in wait["cli"])

    export_wait = describe_operation("exports.wait")
    assert "opaque or Python-only parameters" in export_wait["invocation_note"]
    assert "generic JSON request executor" in export_wait["invocation_note"]

    download = describe_operation("exports.download")
    assert download["kind"] == "sdk_method"
    assert download["indirect_note"].startswith("This method also streams")


def test_describe_rejects_ambiguous_cli_and_unknown_aliases():
    with pytest.raises(ValueError, match="ambiguous CLI alias"):
        describe_operation("export.project")
    with pytest.raises(ValueError, match="unknown operation"):
        describe_operation("not.a.real.operation")
    command = describe_operation("tui")
    assert command["kind"] == "cli_command"
    assert command["api_operations"] == []


def test_capabilities_returns_detached_tags():
    first = list_capabilities(limit=1)
    first["operations"][0]["tags"].append("mutated")
    second = list_capabilities(limit=1)
    assert "mutated" not in second["operations"][0]["tags"]


@pytest.mark.skipif(
    not SNAPSHOT.is_file(), reason="catalog generation is monorepo-only"
)
def test_generated_catalog_is_current_and_deterministic():
    env = os.environ.copy()
    source = str(PACKAGE_ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_installed_catalog_is_json_and_does_not_embed_openapi_snapshot():
    catalog_path = (
        PACKAGE_ROOT / "src" / "ai_annotation" / "_operation_catalog.generated.json"
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["operation_count"] == len(catalog["operations"])
    assert "paths" not in catalog
    assert "components" not in catalog


def test_core_discovery_import_does_not_load_cli_modules():
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PACKAGE_ROOT / "src"), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ai_annotation.discovery, sys; assert not any(name == 'ai_annotation.cli' or name.startswith('ai_annotation.cli.') for name in sys.modules)",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
