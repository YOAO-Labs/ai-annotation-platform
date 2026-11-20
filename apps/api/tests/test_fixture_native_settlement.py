"""The native fixture supervisor owns suite settlement and child credentials."""

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from fixture_native_business import _business_run_settled  # noqa: E402
from fixture_native_playwright import (  # noqa: E402
    _api_environment,
    _native_run_evidence_settled,
)

RETIRED = {"state": "retired"}
RUN = uuid.UUID("5b0f0f0e-6f8b-4a3d-9f47-5c1d2e3f4a5b")
TARGET = {"run_id": str(RUN), "state": "retired"}


def test_native_api_child_cannot_inherit_supervisor_database_credentials():
    runtime, owner = "postgresql://runtime/db", "postgresql://owner/db"
    child = _api_environment(
        {
            "DATABASE_URL": runtime,
            "MIGRATION_DATABASE_URL": owner,
            "PLAYWRIGHT_E2E_DATABASE_URL": owner,
            "TEST_DATABASE_URL": owner,
        },
        "http://127.0.0.1:9001",
        "media-token",
    )
    assert child["DATABASE_URL"] == runtime
    assert owner not in child.values()


def test_identity_without_receipt_directory_is_unsettled():
    assert _native_run_evidence_settled(RETIRED, RETIRED, False) is False


def test_missing_receipt_directory_without_identity_is_settled():
    assert _native_run_evidence_settled(None, None, False) is True


def test_receipt_directory_without_identity_is_unsettled():
    assert _native_run_evidence_settled(None, None, True) is False


def test_retired_identity_with_receipt_directory_is_settled():
    assert _native_run_evidence_settled(RETIRED, RETIRED, True) is True


def test_live_or_partial_identity_is_unsettled():
    assert _native_run_evidence_settled({"state": "ready"}, RETIRED, True) is False
    assert _native_run_evidence_settled(RETIRED, None, True) is False
    assert _native_run_evidence_settled(None, RETIRED, True) is False


def test_business_settles_identity_only_child_without_native_target():
    assert _business_run_settled("retired", RUN, None, None, None) is True


def test_business_refuses_identity_only_child_with_stale_target():
    assert _business_run_settled("retired", RUN, None, None, TARGET) is False


def test_business_refuses_unretired_child():
    assert _business_run_settled("sealing", RUN, None, None, None) is False
    assert _business_run_settled("sealing", RUN, RETIRED, RETIRED, TARGET) is False


def test_business_requires_target_for_native_child():
    assert _business_run_settled("retired", RUN, RETIRED, RETIRED, TARGET) is True
    assert _business_run_settled("retired", RUN, RETIRED, RETIRED, None) is False
    assert (
        _business_run_settled("retired", RUN, RETIRED, RETIRED, {"state": "ready"})
        is False
    )
    other = {"run_id": str(uuid.uuid4()), "state": "retired"}
    assert _business_run_settled("retired", RUN, RETIRED, RETIRED, other) is False
