"""batch 状态机迁移的角色权限守卫。

从 services/batch.py 抽出的纯判定逻辑(无 DB 依赖):逆向迁移白名单
REVERSE_TRANSITIONS 与角色门禁函数。语法层(VALID_TRANSITIONS)仍由 batch.py
的 transition() 检查;本模块只做角色权限判定。batch.py 经 re-export 保持
`from app.services.batch import ...` 旧入口不变。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, or_

from app.db.enums import BatchStatus, ProjectRole
from app.db.models.project import Project
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.project_access import is_privileged_for_project

# v0.7.3：owner-only 逆向迁移白名单。命中时必须传 reason（1-500 字），写入 audit_log.detail_json.reason。
REVERSE_TRANSITIONS: set[tuple[str, str]] = {
    (BatchStatus.ARCHIVED, BatchStatus.ACTIVE),
    (BatchStatus.APPROVED, BatchStatus.REVIEWING),
    (BatchStatus.REJECTED, BatchStatus.REVIEWING),
    # v0.9.5：丢弃 AI 预标 predictions 重置（owner 兜底，需 reason）
    (BatchStatus.PRE_ANNOTATED, BatchStatus.ACTIVE),
}


def allows_bulk_preannotation(batch: TaskBatch) -> bool:
    """issue #124 · 批量预标注的批次准入规则（API 与 worker 复校验共用）。

    active 批次照旧放行；另放行「尚未分派标注员与质检员」的 draft 批次，
    让管理员在分派人工前先跑 AI。已分派人员的 draft 与已进入人工流程的
    批次（annotating / reviewing / ...）一律拒绝：分派即代表人工工作已定，
    不允许绕过任务锁与生命周期往进行中的工作灌预测。
    """
    if batch.status == BatchStatus.ACTIVE:
        return True
    if batch.status == BatchStatus.DRAFT:
        return batch.annotator_id is None and batch.reviewer_id is None
    return False


def bulk_preannotation_eligible_condition():
    """SQL 侧等价于 `allows_bulk_preannotation` 的过滤条件（聚合 / 列表查询复用）。

    与纯函数判定必须同源：`active` 或「未分派标注员与质检员」的 `draft`。管理员
    从「先跑 AI 再分派」改为「先分派再跑 AI」时，两处都要同步修改。
    """
    return or_(
        TaskBatch.status == BatchStatus.ACTIVE,
        and_(
            TaskBatch.status == BatchStatus.DRAFT,
            TaskBatch.annotator_id.is_(None),
            TaskBatch.reviewer_id.is_(None),
        ),
    )


# v0.7.0：transition 鉴权矩阵 — (from, to) 元组 → 允许角色集合 / 特殊判定
# 'owner' = super_admin 或项目 owner（require_project_owner 等价）
# 'reviewer' = super_admin / project_admin(owner) / reviewer
# 'annotator_assigned' = 标注员且 user_id == batch.annotator_id（v0.7.2 单值语义）
def _is_owner(user: User, project: Project) -> bool:
    """Privileged owner for the batch state machine.

    Kept as the batch-domain name (re-exported through ``services/batch.py``
    alongside ``_is_reviewer`` / ``_is_annotator_assigned`` and covered by the
    batch permission tests) but delegates to the canonical
    :func:`project_access.is_privileged_for_project` so the state machine and
    the request resolver can never drift on the owner/platform-role rule.
    """

    return is_privileged_for_project(user, project)


def _is_reviewer(user: User, project: Project, project_role: str | None = None) -> bool:
    """Reviewer authority comes solely from the resolved project role.

    A missing ``project_role`` fails closed; the legacy global staff role is
    never an authority source.  Callers under the project-scoped model must
    pass the resolved membership role.
    """

    if _is_owner(user, project):
        return True
    return project_role == ProjectRole.REVIEWER.value


def _is_annotator_assigned(
    user: User, batch: TaskBatch, project_role: str | None = None
) -> bool:
    """v0.7.2：单值语义 — batch.annotator_id == user.id。"""

    if project_role != ProjectRole.ANNOTATOR.value:
        return False
    return batch.annotator_id is not None and batch.annotator_id == user.id


def assert_can_transition(
    user: User,
    project: Project,
    batch: TaskBatch,
    target_status: str,
    *,
    project_role: str | None = None,
) -> None:
    """v0.7.0：按 (from, to) 校验角色权限，403 携带可读 detail。
    语法层（VALID_TRANSITIONS）由 transition() 内部检查；本函数只做角色门禁。

    项目化后由调用方传入已解析的 ``project_role``（``batch.py``/``api/v1/batches.py``
    由 B2 接线）。``project_role=None`` 对非 owner 一律 fail closed，不回退全局角色。
    """
    src = batch.status
    dst = target_status

    # v0.7.3：逆向迁移（撤销归档 / 重开审核 / 跳标复审）owner-only，非 owner 直接拒
    if (src, dst) in REVERSE_TRANSITIONS:
        if not _is_owner(user, project):
            raise HTTPException(
                status_code=403,
                detail=f"{user.role} cannot reverse-transition {src} -> {dst}",
            )
        return

    # owner / super_admin 始终放行（包含 archived 出口、rejected 重激活）
    if _is_owner(user, project):
        return

    # draft → active：仅 owner（已被上面拦截）；其他角色拒绝
    if (src, dst) == (BatchStatus.DRAFT, BatchStatus.ACTIVE):
        raise HTTPException(
            status_code=403, detail=f"{user.role} cannot transition draft -> active"
        )

    # active → annotating：仅 check_auto_transitions 内部驱动；REST 一律拒绝
    if (src, dst) == (BatchStatus.ACTIVE, BatchStatus.ANNOTATING):
        raise HTTPException(
            status_code=403, detail="active -> annotating is auto-driven only"
        )

    # v0.9.5：active → pre_annotated 仅 batch_predict task 内部驱动；REST 一律拒绝
    if (src, dst) == (BatchStatus.ACTIVE, BatchStatus.PRE_ANNOTATED):
        raise HTTPException(
            status_code=403, detail="active -> pre_annotated is auto-driven only"
        )

    # v0.9.5：pre_annotated → annotating 与 active 同语义，scheduler 内部驱动
    if (src, dst) == (BatchStatus.PRE_ANNOTATED, BatchStatus.ANNOTATING):
        raise HTTPException(
            status_code=403, detail="pre_annotated -> annotating is auto-driven only"
        )

    # annotating → reviewing：标注员（被分派）可主动整批提交质检
    if (src, dst) == (BatchStatus.ANNOTATING, BatchStatus.REVIEWING):
        if _is_annotator_assigned(user, batch, project_role):
            return
        raise HTTPException(
            status_code=403,
            detail=f"{user.role} cannot transition annotating -> reviewing",
        )

    # active / annotating → rejected：整批退回（reviewer / owner）。批次尚未整批送审
    # （review_tasks > 0 但状态仍是 active/annotating）时，审核页同样提供整批退回入口。
    # 通用 /transition 仍受 VALID_TRANSITIONS 限制，只有专用 /reject 端点会执行该迁移。
    if dst == BatchStatus.REJECTED and src in (
        BatchStatus.ACTIVE,
        BatchStatus.ANNOTATING,
    ):
        if _is_reviewer(user, project, project_role):
            return
        raise HTTPException(
            status_code=403, detail=f"{user.role} cannot reject batch from {src}"
        )

    # reviewing → approved / rejected：reviewer
    if src == BatchStatus.REVIEWING and dst in (
        BatchStatus.APPROVED,
        BatchStatus.REJECTED,
    ):
        if _is_reviewer(user, project, project_role):
            return
        raise HTTPException(
            status_code=403, detail=f"{user.role} cannot transition reviewing -> {dst}"
        )

    # rejected → active：仅 owner（已被上面拦截）
    if (src, dst) == (BatchStatus.REJECTED, BatchStatus.ACTIVE):
        raise HTTPException(
            status_code=403, detail=f"{user.role} cannot reactivate rejected batch"
        )

    # 任意 → archived：仅 owner（已被上面拦截）
    if dst == BatchStatus.ARCHIVED:
        raise HTTPException(status_code=403, detail=f"{user.role} cannot archive batch")

    # approved → 其他：仅 archived 合法（VALID_TRANSITIONS 已限），owner 已放行；其他拒
    raise HTTPException(
        status_code=403, detail=f"{user.role} cannot transition {src} -> {dst}"
    )
