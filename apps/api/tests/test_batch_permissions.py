"""batch 状态机权限守卫纯函数表征测试。

守护 services/batch.py 的 _is_owner / _is_reviewer / _is_annotator_assigned /
assert_can_transition —— 这些是无 DB 依赖的纯判定函数,可直接构造 ORM 实例测试。
作为「巨石拆分 Epic」缓拆项补测试再拆的守护网:本文件先对当前代码绿,
后续把这 4 个函数搬到 batch_permissions.py 后(batch.py re-export),本文件一字不改仍绿。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.db.enums import BatchStatus, UserRole
from app.db.models.project import Project
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.batch import (
    REVERSE_TRANSITIONS,
    VALID_TRANSITIONS,
    _is_annotator_assigned,
    _is_owner,
    _is_reviewer,
    assert_can_transition,
)


def _user(role: UserRole, uid: uuid.UUID | None = None) -> User:
    return User(id=uid or uuid.uuid4(), role=role, is_active=True)


def _project(owner_id: uuid.UUID) -> Project:
    return Project(owner_id=owner_id)


def _batch(status: str, annotator_id: uuid.UUID | None = None) -> TaskBatch:
    return TaskBatch(status=status, annotator_id=annotator_id)


# 全部 (src, dst) 合法边,从 VALID_TRANSITIONS 展开,用于矩阵参数化。
ALL_EDGES = [(src, dst) for src, dsts in VALID_TRANSITIONS.items() for dst in dsts]


# ── _is_owner ──────────────────────────────────────────────────────────────


class TestIsOwner:
    def test_super_admin_is_always_owner(self):
        user = _user(UserRole.SUPER_ADMIN)
        # owner_id 是别人,super_admin 仍判定为 owner
        assert _is_owner(user, _project(uuid.uuid4())) is True

    def test_project_owner_is_owner(self):
        user = _user(UserRole.PROJECT_ADMIN)
        assert _is_owner(user, _project(user.id)) is True

    def test_non_admin_owner_is_not_owner(self):
        # An anomalous non-administrative owner is not privileged.
        user = _user(UserRole.EMPLOYEE)
        assert _is_owner(user, _project(user.id)) is False

    def test_non_owner_non_admin_is_not_owner(self):
        user = _user(UserRole.EMPLOYEE)
        assert _is_owner(user, _project(uuid.uuid4())) is False


# ── _is_reviewer ───────────────────────────────────────────────────────────


class TestIsReviewer:
    def test_super_admin_is_reviewer(self):
        user = _user(UserRole.SUPER_ADMIN)
        assert _is_reviewer(user, _project(uuid.uuid4())) is True

    def test_project_owner_is_reviewer(self):
        user = _user(UserRole.PROJECT_ADMIN)
        assert _is_reviewer(user, _project(user.id)) is True

    def test_reviewer_project_role_is_reviewer(self):
        user = _user(UserRole.EMPLOYEE)
        assert _is_reviewer(user, _project(uuid.uuid4()), "reviewer") is True

    def test_missing_project_role_fails_closed(self):
        # A legacy global reviewer value is not an authority source.
        user = _user(UserRole.REVIEWER)
        assert _is_reviewer(user, _project(uuid.uuid4())) is False

    def test_plain_annotator_role_is_not_reviewer(self):
        user = _user(UserRole.EMPLOYEE)
        assert _is_reviewer(user, _project(uuid.uuid4()), "annotator") is False


# ── _is_annotator_assigned ──────────────────────────────────────────────────


class TestIsAnnotatorAssigned:
    def test_assigned_annotator_matches(self):
        user = _user(UserRole.EMPLOYEE)
        assert (
            _is_annotator_assigned(user, _batch("annotating", user.id), "annotator")
            is True
        )

    def test_non_annotator_role_never_assigned(self):
        user = _user(UserRole.EMPLOYEE)
        assert (
            _is_annotator_assigned(user, _batch("annotating", user.id), "reviewer")
            is False
        )

    def test_missing_project_role_fails_closed(self):
        user = _user(UserRole.ANNOTATOR)
        assert _is_annotator_assigned(user, _batch("annotating", user.id)) is False

    def test_unassigned_batch_is_false(self):
        user = _user(UserRole.EMPLOYEE)
        assert (
            _is_annotator_assigned(user, _batch("annotating", None), "annotator")
            is False
        )

    def test_other_annotator_assigned_is_false(self):
        user = _user(UserRole.EMPLOYEE)
        assert (
            _is_annotator_assigned(
                user, _batch("annotating", uuid.uuid4()), "annotator"
            )
            is False
        )


# ── assert_can_transition ───────────────────────────────────────────────────


def _assert_allowed(user, project, batch, dst, project_role=None):
    # 不抛异常即放行
    assert (
        assert_can_transition(user, project, batch, dst, project_role=project_role)
        is None
    )


def _assert_denied(user, project, batch, dst, project_role=None):
    with pytest.raises(HTTPException) as exc:
        assert_can_transition(user, project, batch, dst, project_role=project_role)
    assert exc.value.status_code == 403


class TestAssertCanTransitionOwner:
    @pytest.mark.parametrize("src,dst", ALL_EDGES)
    def test_super_admin_allows_every_valid_edge(self, src, dst):
        user = _user(UserRole.SUPER_ADMIN)
        project = _project(uuid.uuid4())
        _assert_allowed(user, project, _batch(src), dst)

    @pytest.mark.parametrize("src,dst", ALL_EDGES)
    def test_project_owner_allows_every_valid_edge(self, src, dst):
        # 管理型 owner 应放行所有合法边
        user = _user(UserRole.PROJECT_ADMIN)
        project = _project(user.id)
        _assert_allowed(user, project, _batch(src, user.id), dst)


class TestAssertCanTransitionReviewer:
    # reviewer(非 owner)仅在 reviewing → approved / rejected 放行,其余全 403。
    @pytest.mark.parametrize("src,dst", ALL_EDGES)
    def test_reviewer_matrix(self, src, dst):
        user = _user(UserRole.EMPLOYEE)
        project = _project(uuid.uuid4())
        batch = _batch(src)
        if src == BatchStatus.REVIEWING and dst in (
            BatchStatus.APPROVED,
            BatchStatus.REJECTED,
        ):
            _assert_allowed(user, project, batch, dst, project_role="reviewer")
        else:
            _assert_denied(user, project, batch, dst, project_role="reviewer")


class TestAssertCanTransitionAnnotator:
    def test_assigned_annotator_can_submit_for_review(self):
        user = _user(UserRole.EMPLOYEE)
        project = _project(uuid.uuid4())
        batch = _batch(BatchStatus.ANNOTATING, user.id)
        _assert_allowed(
            user, project, batch, BatchStatus.REVIEWING, project_role="annotator"
        )

    def test_unassigned_annotator_cannot_submit_for_review(self):
        user = _user(UserRole.EMPLOYEE)
        project = _project(uuid.uuid4())
        batch = _batch(BatchStatus.ANNOTATING, None)
        _assert_denied(
            user, project, batch, BatchStatus.REVIEWING, project_role="annotator"
        )

    @pytest.mark.parametrize("src,dst", ALL_EDGES)
    def test_assigned_annotator_denied_everywhere_else(self, src, dst):
        # 被分派标注员除 annotating → reviewing 外,其余合法边一律 403。
        if (src, dst) == (BatchStatus.ANNOTATING, BatchStatus.REVIEWING):
            pytest.skip("唯一放行边,由专门用例覆盖")
        user = _user(UserRole.EMPLOYEE)
        project = _project(uuid.uuid4())
        batch = _batch(src, user.id)
        _assert_denied(user, project, batch, dst, project_role="annotator")


class TestAssertCanTransitionReverse:
    # 逆向迁移 owner-only:非 owner 一律 403,owner 放行。
    @pytest.mark.parametrize("src,dst", sorted(REVERSE_TRANSITIONS))
    def test_reverse_denied_for_reviewer(self, src, dst):
        user = _user(UserRole.EMPLOYEE)
        _assert_denied(
            user, _project(uuid.uuid4()), _batch(src), dst, project_role="reviewer"
        )

    @pytest.mark.parametrize("src,dst", sorted(REVERSE_TRANSITIONS))
    def test_reverse_allowed_for_super_admin(self, src, dst):
        user = _user(UserRole.SUPER_ADMIN)
        _assert_allowed(user, _project(uuid.uuid4()), _batch(src), dst)


class TestAllowsBulkPreannotation:
    """issue #124 · 批量预标注批次准入: active 或未分派人员的 draft。"""

    @staticmethod
    def _batch(status: str, *, annotator_id=None, reviewer_id=None) -> TaskBatch:
        return TaskBatch(
            status=status, annotator_id=annotator_id, reviewer_id=reviewer_id
        )

    def test_active_always_allowed(self):
        from app.services.batch_permissions import allows_bulk_preannotation

        assert allows_bulk_preannotation(self._batch(BatchStatus.ACTIVE))
        assert allows_bulk_preannotation(
            self._batch(BatchStatus.ACTIVE, annotator_id=uuid.uuid4())
        )

    def test_unassigned_draft_allowed(self):
        from app.services.batch_permissions import allows_bulk_preannotation

        assert allows_bulk_preannotation(self._batch(BatchStatus.DRAFT))

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"annotator_id": "annotator"},
            {"reviewer_id": "reviewer"},
            {"annotator_id": "annotator", "reviewer_id": "reviewer"},
        ],
    )
    def test_assigned_draft_denied(self, kwargs):
        from app.services.batch_permissions import allows_bulk_preannotation

        ids = {"annotator": uuid.uuid4(), "reviewer": uuid.uuid4()}
        resolved = {k: ids[v] for k, v in kwargs.items()}
        assert not allows_bulk_preannotation(self._batch(BatchStatus.DRAFT, **resolved))

    @pytest.mark.parametrize(
        "status",
        [
            BatchStatus.PRE_ANNOTATED,
            BatchStatus.ANNOTATING,
            BatchStatus.REVIEWING,
            BatchStatus.APPROVED,
            BatchStatus.REJECTED,
            BatchStatus.ARCHIVED,
        ],
    )
    def test_manual_lifecycle_statuses_denied(self, status):
        from app.services.batch_permissions import allows_bulk_preannotation

        assert not allows_bulk_preannotation(self._batch(status))
