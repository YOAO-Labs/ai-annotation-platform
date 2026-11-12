"""Prepare the existing filtering fixture once for an owned manual E2E session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import urlencode
from uuid import UUID, uuid4

from worktree_env import (
    WorktreeError,
    atomic_json,
    load_identity,
    read_state,
    state_path,
    topology,
)

MANUAL_NAMESPACE = "manualfilter"
_RETAINED_CLIENTS = []


async def _fixture_request(payload: dict):
    script = (
        Path(__file__).resolve().parents[1] / "apps/web/e2e/fixtures/seed-request.py"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    operation_id = str(uuid4())
    response = None
    try:
        process.stdin.write((json.dumps({
            "action": "execute", "operation_id": operation_id, "payload": payload,
        }) + "\n").encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise WorktreeError("筛选夹具 owner 未返回操作结果")
        response = json.loads(line)
        if response.get("operation_id") != operation_id or response.get("state") == "pending":
            raise WorktreeError("筛选夹具操作结果尚未确定")
        if (
            ("owner_error" in response and (
                not isinstance(response["owner_error"], str) or not response["owner_error"].isidentifier()
            ))
            or ("owner_error" not in response and (
                type(response.get("status")) is not int or not isinstance(response.get("body"), str)
            ))
            or ("journal_error" in response and (
                not isinstance(response["journal_error"], str) or not response["journal_error"].isidentifier()
            ))
            or ("disposal_error" in response and (
                not isinstance(response["disposal_error"], str) or not response["disposal_error"].isidentifier()
            ))
            or ("operation_response" in response and (
                not isinstance(response["operation_response"], dict)
                or type(response["operation_response"].get("status")) is not int
                or not isinstance(response["operation_response"].get("body"), str)
            ))
        ):
            raise WorktreeError("筛选夹具 owner 返回了无效结果")
        if response.get("owner_error") not in {
            "OperationOutcomeUnresolved", "OperationIdentityConflict"
        }:
            process.stdin.write((json.dumps({
                "action": "ack", "operation_id": operation_id,
            }) + "\n").encode())
            await process.stdin.drain()
            receipt = json.loads(await process.stdout.readline())
            if receipt != {"operation_id": operation_id, "state": "acknowledged"}:
                raise WorktreeError("筛选夹具操作回执尚未确定")
        process.stdin.close()
        stderr = await process.stderr.read()
        await process.wait()
    except BaseException as error:
        _RETAINED_CLIENTS.append(process)
        if response is not None:
            error.fixture_response = response
            error.add_note("Fixture operation response was received before transport failure")
        error.fixture_operation_id = operation_id
        error.add_note(f"Fixture operation ID: {operation_id}")
        raise
    journal = ".worktree/e2e/fixture-authority/failures.ndjson"
    if process.returncode != 0:
        matches = re.findall(rb"^Fixture setup failed: (\w+)$", stderr, re.MULTILINE)
        kind = matches[-1].decode() if matches else "unknown"
        raise WorktreeError(
            f"筛选夹具 owner 进程失败：exit {process.returncode}, {kind}；私有证据：{journal}"
        )
    if response.get("owner_error") or response.get("journal_error"):
        causes = []
        if response.get("operation_response"):
            causes.append(WorktreeError(f"筛选夹具准备结果：HTTP {response['operation_response']['status']}"))
        if response.get("owner_error") and response["owner_error"] != response.get("disposal_error"):
            causes.append(WorktreeError(f"筛选夹具操作失败：{response['owner_error']}"))
        if response.get("disposal_error"):
            causes.append(WorktreeError(f"筛选夹具数据库释放失败：{response['disposal_error']}"))
        if not causes:
            causes.append(WorktreeError(f"筛选夹具准备失败：HTTP {response['status']}"))
        if response.get("journal_error"):
            causes.append(WorktreeError(f"私有证据写入失败：{response['journal_error']}"))
        error = causes[0] if len(causes) == 1 else ExceptionGroup("夹具操作与证据失败", causes)
        error.fixture_response = response
        error.fixture_operation_id = operation_id
        error.add_note(f"Fixture operation ID: {operation_id}")
        raise error
    if response["status"] != 200:
        code = "unknown"
        try:
            detail = json.loads(response["body"]).get("detail")
            if isinstance(detail, dict):
                value = detail.get("code", detail.get("reason"))
                if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value):
                    code = value
        except (ValueError, AttributeError):
            pass
        raise WorktreeError(
            f"筛选夹具准备失败：HTTP {response['status']}, {code}；私有证据：{journal}"
        )
    return json.loads(response["body"])


async def _seed_filtering(suite_id: UUID, run_id: UUID):
    return await _fixture_request(
        {
            "path": "/api/v1/__test/seed/filtering",
            "data": {
                "namespace": MANUAL_NAMESPACE,
                "suite_id": str(suite_id),
                "run_id": str(run_id),
            },
        }
    )


def acceptance_guide(manifest: dict) -> dict:
    image, video = manifest["image"], manifest["video"]

    prefix, _, domain = manifest["user_emails"]["admin"].partition("@")
    namespace = prefix.removeprefix("admin-") if prefix.startswith("admin-") else None
    if namespace and domain == "e2e.test":
        from app.api.v1._test_seed_filters import (
            _FILTER_OWNED_IDS,
            _FILTER_OWNED_PARAMETRIC,
        )

        def display(base, *args):
            template = _FILTER_OWNED_PARAMETRIC.get(base) or _FILTER_OWNED_IDS[base]
            return template.format(*args, ns=namespace)
    else:

        def display(base, *args):
            return base.format(*args)

    same = display("T-E2E-FILTER-I-SAME")
    cross = display("T-E2E-FILTER-I-CROSS")
    hidden = display("T-E2E-FILTER-I-HIDDEN")
    missing = display("T-E2E-FILTER-I-MISSING")
    videos = [display("T-E2E-FILTER-V-{0}", index) for index in range(1, 5)]

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
                "expected": f"标注员初始仅命中 {same}；{cross} 的红车和蓝色行人不能拼成蓝车。管理员还会看到 {hidden}。",
            },
            {
                "title": "必填属性与嵌套条件",
                "path": manager(
                    image["project_id"], view="builtin:missing-required-attributes"
                ),
                "expected": f"初始仅命中 {missing}；可编辑 AND/OR、保存视图并刷新验证。",
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
                "expected": f"初始命中 {videos[0]}、{videos[1]}、{videos[2]}，共 3 题；只有已采纳/驳回候选的 {videos[3]} 不命中。",
            },
            {
                "title": "视频当前帧与批量范围",
                "path": f"/projects/{video['project_id']}/annotate?task={video['task_ids']['both']}&frame=0",
                "expected": f"标注员打开 {videos[2]}，将置信度阈值调到 0；F0 与 F10 各有一个检测候选，批量采纳范围为已加载的两帧。",
            },
            {
                "title": "管理列表",
                "path": "/dashboard?q=Filter",
                "expected": "使用管理员检查 Filter 项目、数据集、模板、成员、邀请、审计与任务状态；刷新或前进后退应恢复条件。",
            },
        ],
    }


async def prepare_filtering(resources: dict, manifest_path: Path) -> dict:
    from sqlalchemy import select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1._test_seed import _require_e2e_seed_database
    from app.api.v1._test_seed_filters import FilteringSeedManifest
    from app.config import settings
    from app.db.models.fixture_run import FixtureRun, FixtureRunResource
    from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
    from app.services.workspace_seed_data import seed_projects
    from scripts.fixture_suite_coordinator import (
        prepare_fixture_suite,
        verified_fixture_owner_url,
    )

    if os.environ.get("AAP_WORKTREE_MODE") != "e2e" or resources["mode"] != "e2e":
        raise WorktreeError("手动验收必须通过 up --mode e2e --scenario filtering 启动")
    owner_url = await verified_fixture_owner_url()
    runtime_url = make_url(settings.database_url)
    if (
        owner_url.get_backend_name() != "postgresql"
        or owner_url.database != resources["database"]
        or not owner_url.database.endswith(("_e2e", "_test"))
        or (owner_url.host, owner_url.port or 5432, owner_url.database)
        != (runtime_url.host, runtime_url.port or 5432, runtime_url.database)
    ):
        raise WorktreeError("夹具 owner 与运行时数据库不一致，拒绝写入")
    engine = create_async_engine(owner_url, hide_parameters=True)
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
                    document.get("version") != 2
                    or document.get("owner") != resources["owner"]
                ):
                    raise WorktreeError(
                        "验收记录不匹配；请检查当前环境，不会自动重置数据"
                    )
                if document.get("state") != "ready":
                    raise WorktreeError(
                        "筛选夹具上次准备未完成；保留 run 记录并先执行精确清理"
                    )
                suite_id, run_id = UUID(document["suite_id"]), UUID(document["run_id"])
                run = await db.get(FixtureRun, run_id)
                if (
                    run is None
                    or run.parent_run_id != suite_id
                    or run.scope_key != MANUAL_NAMESPACE
                    or run.state != "open"
                ):
                    raise WorktreeError(
                        "筛选夹具 run 身份不匹配；不会采用或覆盖现有数据"
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
                        "验收项目已被删除或测试清理；请先检查精确 run，不会自动覆盖数据"
                    )
                owned_projects = set(
                    await db.scalars(
                        select(FixtureRunResource.resource_id).where(
                            FixtureRunResource.run_id == run_id,
                            FixtureRunResource.kind == "project",
                        )
                    )
                )
                if owned_projects != project_ids:
                    raise WorktreeError("筛选项目不属于记录的 run；拒绝复用")
                print("[dev:worktree] 复用筛选验收数据，保留已有操作", flush=True)
            else:
                suite_id, run_id = uuid4(), uuid4()
                atomic_json(
                    manifest_path,
                    {
                        "version": 2,
                        "owner": resources["owner"],
                        "state": "preparing",
                        "suite_id": str(suite_id),
                        "run_id": str(run_id),
                    },
                )
                await prepare_fixture_suite(str(suite_id), "owned")
                manifest = FilteringSeedManifest.model_validate(
                    await _seed_filtering(suite_id, run_id)
                )
                # Supply renderable geometry without changing the fixture's literal
                # memberships/counts. The automatic tests keep their metric-only data.
                await LabelingE2EFixtureWrites(db).prepare_filtering_rendering(
                    project_id=UUID(manifest.video.project_id),
                    low_prediction_id=UUID(manifest.video.candidate_ids["low"]),
                    at_prediction_id=UUID(manifest.video.candidate_ids["at"]),
                    above_prediction_id=UUID(manifest.video.candidate_ids["above"]),
                )
                await db.commit()
                print("[dev:worktree] 已创建筛选验收数据", flush=True)
            payload = manifest.model_dump(mode="json", by_alias=True)
            document = {
                "version": 2,
                "owner": resources["owner"],
                "state": "ready",
                "suite_id": str(suite_id),
                "run_id": str(run_id),
                "manifest": payload,
                "guide": acceptance_guide(payload),
            }
            atomic_json(manifest_path, document)
            return document
    finally:
        await engine.dispose()


async def cleanup_filtering(resources: dict, manifest_path: Path) -> None:
    """Clean only the suite recorded by this manual session."""
    from scripts.fixture_suite_coordinator import verified_fixture_owner_url

    if os.environ.get("AAP_WORKTREE_MODE") != "e2e" or resources["mode"] != "e2e":
        raise WorktreeError("精确清理要求当前 e2e 工作树")
    if not manifest_path.exists():
        raise WorktreeError("没有可清理的筛选夹具 run 记录")
    document = read_state(manifest_path)
    if document.get("version") != 2 or document.get("owner") != resources["owner"]:
        raise WorktreeError("筛选夹具记录归属不明；拒绝自动清理")
    suite_id = UUID(document["suite_id"])
    await verified_fixture_owner_url()
    await _fixture_request(
        {
            "path": "/api/v1/__test/seed/cleanup",
            "data": {"suite_id": str(suite_id)},
        }
    )
    result = await _fixture_request(
        {"operation": "owner_shutdown", "suite_id": str(suite_id)}
    )
    if result.get("state") not in {"stopped", "shared"}:
        raise WorktreeError("筛选夹具 owner 关闭结果不明；保留记录")
    manifest_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup", action="store_true", help="clean the recorded exact suite"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "apps/api"))
    resources = topology(root, load_identity(root), "e2e")
    path = state_path(root, "e2e", "data", "filtering.json")
    if args.cleanup:
        asyncio.run(cleanup_filtering(resources, path))
    else:
        asyncio.run(prepare_filtering(resources, path))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from worktree_runtime import safe_error

        print(f"[dev:worktree] {safe_error(error)}", file=sys.stderr)
        raise SystemExit(1) from None
