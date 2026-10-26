"""Prepare the existing filtering fixture once for an owned manual E2E session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from uuid import UUID

from worktree_env import (
    WorktreeError,
    atomic_json,
    load_identity,
    read_state,
    state_path,
    topology,
)


def acceptance_guide(manifest: dict) -> dict:
    image, video = manifest["image"], manifest["video"]

    def manager(project: str, **params: str) -> str:
        return f"/projects/{project}/data-manager?{urlencode(params)}"

    same_object = {
        "op": "and",
        "rules": [
            {"field": "annotation.class_name", "op": "eq", "value": "car"},
            {"field": "annotation.attribute.bbox.color", "op": "eq", "value": "blue"},
        ],
    }
    return {
        "accounts": [
            {
                "role": role,
                "email": manifest["user_emails"][key],
                "password": "Test1234",
            }
            for key, role in (
                ("admin", "管理员"),
                ("anno", "标注员"),
                ("rev", "审核员"),
            )
        ],
        "scenarios": [
            {
                "title": "任务属性与权限",
                "path": manager(image["project_id"], lens="tasks"),
                "expected": "初始管理员可见 6 题，标注员可见 5 题；隐藏批次不应出现在标注员的列表或总数中。",
            },
            {
                "title": "同一对象满足 car AND blue",
                "path": manager(
                    image["project_id"],
                    filter=json.dumps({"v": 1, "value": same_object}),
                ),
                "expected": "标注员初始仅命中 T-E2E-FILTER-I-SAME；CROSS 的红车和蓝色行人不能拼成蓝车。管理员还会看到 HIDDEN。",
            },
            {
                "title": "必填属性与嵌套条件",
                "path": manager(
                    image["project_id"], view="builtin:missing-required-attributes"
                ),
                "expected": "初始仅命中 T-E2E-FILTER-I-MISSING；可编辑 AND/OR、保存视图并刷新验证。",
            },
            {
                "title": "对象分页",
                "path": manager(manifest["paging"]["project_id"], lens="objects"),
                "expected": "初始总数 101，首次加载 100；加载更多后为 101，总数保持不变。",
            },
            {
                "title": "逻辑轨迹分页",
                "path": manager(manifest["lidar"]["project_id"], lens="tracks"),
                "expected": "标注员初始可见 101 条逻辑轨迹；管理员另可见隐藏批次中的 1 条。跨任务同一轨迹不应重复计数。",
            },
            {
                "title": "视频 AI 待审 OR 条件",
                "path": manager(video["project_id"], view="builtin:ai-review"),
                "expected": "初始命中 V-1、V-2、V-3，共 3 题；只有已采纳/驳回候选的 V-4 不命中。",
            },
            {
                "title": "视频当前帧与批量范围",
                "path": f"/projects/{video['project_id']}/annotate?task={video['task_ids']['both']}&frame=0",
                "expected": "标注员打开 V-3，将置信度阈值调到 0；F0 与 F10 各有一个检测候选，批量采纳范围为已加载的两帧。",
            },
            {
                "title": "管理列表",
                "path": "/dashboard?q=Filter",
                "expected": "使用管理员检查 Filter 项目、数据集、模板、成员、邀请、审计与任务状态；刷新或前进后退应恢复条件。",
            },
        ],
    }


async def prepare_filtering(resources: dict, manifest_path: Path) -> dict:
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1._test_seed import _require_e2e_seed_database, seed_filtering
    from app.api.v1._test_seed_filters import FilteringSeedManifest
    from app.config import settings
    from app.db.models.dataset import Dataset
    from app.services.identity_read_relations import account_profiles
    from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
    from app.services.workspace_seed_data import seed_projects

    if os.environ.get("AAP_WORKTREE_MODE") != "e2e" or resources["mode"] != "e2e":
        raise WorktreeError("手动验收必须通过 up --mode e2e --scenario filtering 启动")
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await _require_e2e_seed_database(db)
            identity = (
                await db.execute(
                    text(
                        "SELECT current_database(), shobj_description(oid, 'pg_database') "
                        "FROM pg_database WHERE datname = current_database()"
                    )
                )
            ).one()
            if tuple(identity) != (resources["database"], resources["owner"]):
                raise WorktreeError("验收数据库归属不匹配，拒绝写入")
            if manifest_path.exists():
                document = read_state(manifest_path)
                if (
                    document.get("version") != 1
                    or document.get("owner") != resources["owner"]
                ):
                    raise WorktreeError(
                        "验收记录不匹配；请检查当前环境，不会自动重置数据"
                    )
                manifest = FilteringSeedManifest.model_validate(document["manifest"])
                project_ids = {
                    UUID(section.project_id)
                    for section in (
                        manifest.image,
                        manifest.video,
                        manifest.paging,
                        manifest.lidar,
                    )
                } | {UUID(value) for value in manifest.operations.project_ids}
                present = set(
                    (
                        await db.scalars(
                            select(seed_projects.id).where(
                                seed_projects.id.in_(project_ids)
                            )
                        )
                    ).all()
                )
                if present != project_ids:
                    raise WorktreeError(
                        "验收项目已被删除或测试清理；请显式 reset 后重新启动，不会自动覆盖数据"
                    )
                print("[dev:worktree] 复用筛选验收数据，保留已有操作", flush=True)
            else:
                for model in (account_profiles, seed_projects, Dataset):
                    if await db.scalar(select(func.count()).select_from(model)):
                        raise WorktreeError(
                            "e2e 库已有数据但没有手动验收记录；请先确认并 reset --mode e2e"
                        )
                manifest = await seed_filtering(db=db)
                # Supply renderable geometry without changing the fixture's literal
                # memberships/counts. The automatic tests keep their metric-only data.
                await LabelingE2EFixtureWrites(db).prepare_filtering_rendering(
                    project_id=UUID(manifest.video.project_id),
                    low_prediction_id=UUID(
                        manifest.video.candidate_ids["low"]
                    ),
                    at_prediction_id=UUID(manifest.video.candidate_ids["at"]),
                    above_prediction_id=UUID(
                        manifest.video.candidate_ids["above"]
                    ),
                )
                await db.commit()
                print("[dev:worktree] 已创建筛选验收数据", flush=True)
            payload = manifest.model_dump(mode="json", by_alias=True)
            document = {
                "version": 1,
                "owner": resources["owner"],
                "manifest": payload,
                "guide": acceptance_guide(payload),
            }
            atomic_json(manifest_path, document)
            return document
    finally:
        await engine.dispose()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "apps/api"))
    resources = topology(root, load_identity(root), "e2e")
    path = state_path(root, "e2e", "data", "filtering.json")
    asyncio.run(prepare_filtering(resources, path))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from worktree_runtime import safe_error

        print(f"[dev:worktree] {safe_error(error)}", file=sys.stderr)
        raise SystemExit(1) from None
