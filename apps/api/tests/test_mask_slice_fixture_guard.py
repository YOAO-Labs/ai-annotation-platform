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
from uuid import uuid4

import pytest

from app.db.models.fixture_run import FixtureRun, FixtureRunResource
from app.db.models.annotation import Annotation
from tests.factory import create_project, create_task, create_user

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
    root: Path, *, owner: str = "yoao-worktree:abc:e2e:def", mode: str = "e2e"
) -> dict:
    return {
        "root": str(root),
        "mode": mode,
        "owner": owner,
        "database": "yoao_wt_abc_e2e",
        "buckets": {"MINIO_BUCKET": "yoao-wt-abc-e2e-annotations"},
    }


def _repo_root() -> Path:
    return _module._REPO_ROOT


def test_worktree_owned_bucket_passes() -> None:
    assert_owned_disposable_bucket(
        mode="e2e",
        resources=_resources(_repo_root()),
        database="yoao_wt_abc_e2e",
        bucket="yoao-wt-abc-e2e-annotations",
        owner_tag="yoao-worktree:abc:e2e:def",
    )


def test_selected_resource_root_keeps_bucket_owner_checks(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    assert_owned_disposable_bucket(
        mode="e2e",
        resources=resources,
        database="yoao_wt_abc_e2e",
        bucket="yoao-wt-abc-e2e-annotations",
        owner_tag="yoao-worktree:abc:e2e:def",
        resource_root=tmp_path,
    )
    with pytest.raises(GuardError, match="another checkout"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=resources,
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )


@pytest.mark.parametrize("mode", ["dev", "prod", ""])
def test_worktree_rejects_unrecognized_modes(mode: str) -> None:
    with pytest.raises(GuardError, match="refuses mode|requires the active"):
        assert_owned_disposable_bucket(
            mode=mode,
            resources=_resources(_repo_root()),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )


@pytest.mark.parametrize(
    "owner_tag", ["yoao-worktree:other:e2e:zzz", "malformed", "yoao-worktree:abc"]
)
def test_worktree_rejects_foreign_or_malformed_owner_tag(owner_tag: str) -> None:
    with pytest.raises(GuardError):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag=owner_tag,
        )


def test_worktree_rejects_missing_owner_tag() -> None:
    with pytest.raises(GuardError, match="no owner tag"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag=None,
        )


@pytest.mark.parametrize("owner", [None, ""])
def test_worktree_rejects_missing_or_empty_manifest_owner(owner: str | None) -> None:
    with pytest.raises(GuardError, match="no owner tag"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root(), owner=owner),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )


def test_worktree_rejects_manifest_from_another_mode() -> None:
    with pytest.raises(GuardError, match="another mode"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root(), mode="test"),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )


def test_worktree_rejects_wrong_bucket_slot_and_database_and_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(GuardError, match="resource slot"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-datasets",
            owner_tag="yoao-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="database"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(_repo_root()),
            database="yoao_wt_other_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="another checkout"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=_resources(tmp_path),
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
        )


def test_worktree_rejects_unrelated_and_missing_manifest() -> None:
    with pytest.raises(GuardError, match="resource slot"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources={
                **_resources(_repo_root()),
                "buckets": {"MINIO_BUCKET": "yoao-wt-abc-e2e-annotations"},
            },
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-unrelated",
            owner_tag="yoao-worktree:abc:e2e:def",
        )
    with pytest.raises(GuardError, match="manifest"):
        assert_owned_disposable_bucket(
            mode="e2e",
            resources=None,
            database="yoao_wt_abc_e2e",
            bucket="yoao-wt-abc-e2e-annotations",
            owner_tag="yoao-worktree:abc:e2e:def",
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
    for bucket in (
        "annotations",
        "datasets",
        "yoao-wt-abc-test",
        "aap-wt-abc-test",
        "yoao-wt-abc-e2e-annotations",
    ):
        with pytest.raises(GuardError):
            assert_owned_disposable_bucket(
                mode=None,
                resources=None,
                database="annotation_test",
                bucket=bucket,
                owner_tag=None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_root", [False, True])
async def test_lifecycle_rejects_foreign_resources_before_database_access(
    monkeypatch, tmp_path, foreign_root
):
    resources = _resources(tmp_path)
    monkeypatch.setattr(
        _module, "_REPO_ROOT", tmp_path / "other-checkout" if foreign_root else tmp_path
    )
    monkeypatch.setattr(_module, "_RESOURCE_ROOT", tmp_path)
    monkeypatch.setattr(_module, "read_state", lambda _path: {"resources": resources})
    monkeypatch.setattr(_module, "load_identity", lambda _root: "identity")
    monkeypatch.setattr(
        _module,
        "topology",
        lambda *_args: {**resources, "database": "yoao_wt_other_e2e"},
    )
    monkeypatch.setenv(
        "PLAYWRIGHT_E2E_DATABASE_URL",
        "postgresql+asyncpg://owner@localhost/yoao_wt_abc_e2e",
    )
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("E2E_SEED_ENABLED", "true")
    monkeypatch.setenv("YOAO_WORKTREE_MODE", "e2e")
    reason = "inside this checkout" if foreign_root else "manifest does not match"
    with pytest.raises(GuardError, match=reason):
        await _module.run("gc", str(uuid4()), str(uuid4()), str(uuid4()))


@pytest.mark.asyncio
async def test_annotations_must_belong_to_selected_owned_fixture(db_session):
    suite_id, other_suite, run_id, other_run = (uuid4() for _ in range(4))
    namespace = uuid4().hex[:10]
    for root in (suite_id, other_suite):
        db_session.add(FixtureRun(id=root, family="owned"))
    await db_session.flush()
    for parent, child in ((suite_id, run_id), (other_suite, other_run)):
        db_session.add(
            FixtureRun(
                id=child, parent_run_id=parent, scope_key=namespace, family="owned"
            )
        )
    await db_session.flush()
    user = await create_user(
        db_session, "employee", f"{namespace}@test.invalid", "Mask guard"
    )
    project = await create_project(db_session, owner_id=user.id)
    other_project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    other_task = await create_task(db_session, project_id=other_project.id)
    for child, owned_project, owned_task in (
        (run_id, project, task),
        (other_run, other_project, other_task),
    ):
        for kind, resource, key in (
            ("project", owned_project, "workbench"),
            ("task", owned_task, "workbench-1"),
        ):
            db_session.add(
                FixtureRunResource(
                    run_id=child,
                    kind=kind,
                    resource_id=resource.id,
                    logical_key=key,
                    state="published",
                )
            )
    annotations = [
        Annotation(
            task_id=owner.id,
            project_id=owner.project_id,
            user_id=user.id,
            annotation_type="bbox",
            class_name="object",
            tool_unit_id="bbox",
            geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
        )
        for owner in (task, task, other_task)
    ]
    db_session.add_all(annotations)
    await db_session.flush()
    source, created, foreign = (annotation.id for annotation in annotations)
    await _module.require_fixture_annotations(
        db_session, task.id, source, created, run_id, suite_id
    )
    for selected_task, first, second, run, suite in (
        (task.id, source, foreign, run_id, suite_id),
        (other_task.id, source, created, run_id, suite_id),
        (task.id, source, created, other_run, other_suite),
        (task.id, source, created, run_id, other_suite),
        (task.id, source, source, run_id, suite_id),
    ):
        with pytest.raises(GuardError, match="selected fixture"):
            await _module.require_fixture_annotations(
                db_session, selected_task, first, second, run, suite
            )
    published = await db_session.get(FixtureRunResource, (run_id, "task", task.id))
    published.state = "retired"
    await db_session.flush()
    with pytest.raises(GuardError, match="selected fixture"):
        await _module.require_fixture_annotations(
            db_session, task.id, source, created, run_id, suite_id
        )
