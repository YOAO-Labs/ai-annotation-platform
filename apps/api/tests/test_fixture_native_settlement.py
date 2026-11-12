"""Owned suite settlement must reconcile durable identities with receipts."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from fixture_native_playwright import _native_run_evidence_settled  # noqa: E402

RETIRED = {"state": "retired"}


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
