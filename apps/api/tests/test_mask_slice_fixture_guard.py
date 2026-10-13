"""Regression tests for the mask-slice lifecycle fixture's bucket ownership guard.

The fixture (``apps/web/e2e/fixtures/mask-slice-lifecycle.py``) performs
destructive GC injection, so its bucket precondition must fail closed for
shared, foreign-owned, unrelated and unrecognized worktree buckets while still
accepting the legacy isolated-CI naming. These tests exercise the real
``assert_owned_disposable_bucket`` function with controlled metadata.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "web" / "e2e" / "fixtures"
FIXTURE_FILE = FIXTURE_DIR / "mask-slice-lifecycle.py"
assert FIXTURE_FILE.is_file(), FIXTURE_FILE
sys.path.insert(0, str(FIXTURE_DIR))

_spec = importlib.util.spec_from_file_location("mask_slice_lifecycle", FIXTURE_FILE)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

GuardError = _module.GuardError
assert_owned_disposable_bucket = _module.assert_owned_disposable_bucket


def _resources(
    root: Path, *, owner: str = "aap-worktree:abc:e2e:def", mode: str = "e2e"
) -> dict:
    return {
        "root": str(root),
        "mode": mode,
        "owner": owner,
        "database": "aap_wt_abc_e2e",
        "buckets": {"MINIO_BUCKET": "aap-wt-abc-e2e-annotations"},
    }


def _repo_root() -> Path:
    return _module._REPO_ROOT


def test_worktree_owned_bucket_passes() -> None:
    assert_owned_disposable_bucket(
        mode="e2e",
        resources=_resources(_repo_root()),
        database="aap_wt_abc_e2e",
        bucket="aap-wt-abc-e2e-annotations",
        owner_tag="aap-worktree:abc:e2e:def",
    )


@pytest.mark.parametrize("mode", ["dev", "prod", ""])
def test_worktree_rejects_unrecognized_modes(mode: str) -> None:
    with pytest.raises(GuardError, match="refuses mode|requires the active"):
        assert_owned_disposable_bucket(
            mode=mode,
            resources=_resources(_repo_root()),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )


@pytest.mark.parametrize(
    "owner_tag", ["aap-worktree:other:e2e:zzz", "malformed", "aap-worktree:abc"]
)
def test_worktree_rejects_foreign_or_malformed_owner_tag(owner_tag: str) -> None:
    with pytest.raises(GuardError):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag=owner_tag,
        )


def test_worktree_rejects_missing_owner_tag() -> None:
    with pytest.raises(GuardError, match="no owner tag"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag=None,
        )


@pytest.mark.parametrize("owner", [None, ""])
def test_worktree_rejects_missing_or_empty_manifest_owner(owner: str | None) -> None:
    with pytest.raises(GuardError, match="no owner tag"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root(), owner=owner),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )


def test_worktree_rejects_manifest_from_another_mode() -> None:
    with pytest.raises(GuardError, match="another mode"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root(), mode="test"),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )


def test_worktree_rejects_wrong_bucket_slot_and_database_and_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(GuardError, match="resource slot"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-datasets",
            owner_tag="aap-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="database"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="aap_wt_other_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="another checkout"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(tmp_path),
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )


def test_worktree_rejects_unrelated_and_missing_manifest() -> None:
    with pytest.raises(GuardError, match="resource slot"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources={
                **_resources(_repo_root()),
                "buckets": {"MINIO_BUCKET": "aap-wt-abc-e2e-annotations"},
            },
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-unrelated",
            owner_tag="aap-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="manifest"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=None,
            database="aap_wt_abc_e2e",
            bucket="aap-wt-abc-e2e-annotations",
            owner_tag="aap-worktree:abc:e2e:def",
        )


def test_non_worktree_keeps_legacy_ci_rule_and_rejects_unrecognized_worktree_bucket() -> (
    None
):
    for bucket in ("annotations-test", "annotations_e2e", "datasets-test"):
        assert_owned_disposable_bucket(
            mode=None,
            resources=None,
            database="annotation_test",
            bucket=bucket,
            owner_tag=None,
        )
    for bucket in ("annotations", "datasets", "aap-wt-abc-e2e-annotations"):
        with pytest.raises(GuardError):
            assert_owned_disposable_bucket(
                mode=None,
                resources=None,
                database="annotation_test",
                bucket=bucket,
                owner_tag=None,
            )
