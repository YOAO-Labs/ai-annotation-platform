"""aap CLI 入口: 命令组装。

需要 cli extras: pip install 'ai-annotation-sdk[cli]'。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from typing import Any

import typer

try:
    from typer._click.exceptions import ClickException, MissingParameter
except ImportError:  # Typer < 0.24 uses the installed Click package.
    from click.exceptions import ClickException, MissingParameter

from ai_annotation import __aap_target_version__, __version__
from ai_annotation.cli import (
    annotations,
    batches,
    dashboard,
    data_manager,
    datasets,
    discovery,
    export,
    feedbacks,
    jobs,
    learning,
    login,
    members,
    ml_backends,
    ml_registry,
    plugins,
    notifications,
    pipelines,
    predictions,
    projects,
    service_pools,
    storage,
    tasks,
    templates,
    video_tracker,
    video_review,
    videos,
    files,
    scenes,
    health,
    auth,
    ml_capabilities,
    system_settings,
    account,
    api_keys,
    users,
    groups,
    invitations,
    storage_connections,
    admin_operations,
    ml_diagnostics,
    project_ml,
    audit_logs,
    guide_assets,
    project_performance,
    search,
    streams,
    task_context,
    image_pyramids,
    cross_frame_jobs,
    task_locks,
    point_cloud,
    scene_tracks,
    annotation_operations,
    failed_predictions,
    task_views,
    comments,
    annotation_history,
    bug_reports,
    mask_formats,
    mask_qc,
    point_cloud_quality,
)
from ai_annotation.cli._output import (
    cli_errors,
    console,
    error_format_context,
    get_client,
    get_error_format,
    print_json,
    print_structured_error,
    sparkline,
)

# 让 -h 等价 --help (root 设置经 Click context 继承到所有子命令)
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _requested_error_format(args: Sequence[str]) -> str:
    error_format = "text"
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--error-format" and index + 1 < len(args):
            error_format = "json" if args[index + 1] == "json" else "text"
            index += 2
            continue
        if arg.startswith("--error-format="):
            error_format = "json" if arg.partition("=")[2] == "json" else "text"
            index += 1
            continue
        if arg in {"-h", "--help", "--version"}:
            index += 1
            continue
        break
    return error_format


class _ErrorReportingGroup(typer.core.TyperGroup):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        argv = tuple(sys.argv[1:]) if args is None else tuple(args)
        with error_format_context(_requested_error_format(argv)):
            return super().main(
                args=argv,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                **extra,
            )

    def _report_usage_error(self, error: ClickException) -> None:
        if get_error_format() == "json":
            param = getattr(error, "param", None)
            fields = None
            if param is not None:
                options = getattr(param, "opts", ()) or ()
                name = next(
                    (option[2:] for option in options if option.startswith("--")),
                    getattr(param, "name", None),
                )
                error_type = (
                    "missing"
                    if isinstance(error, MissingParameter)
                    else getattr(getattr(param, "type", None), "name", None)
                )
                if isinstance(error_type, str):
                    error_type = (
                        re.sub(r"[^A-Za-z0-9_]+", "_", error_type).strip("_").lower()
                    )
                    error_type = {"int_range": "integer_range"}.get(
                        error_type, error_type
                    )
                if (
                    isinstance(name, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name)
                    and isinstance(error_type, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", error_type)
                ):
                    fields = [{"loc": [name], "type": error_type}]
            print_structured_error(
                "usage_error", detail="Invalid command usage.", fields=fields
            )
            raise typer.Exit(code=2) from None

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except ClickException as error:
            self._report_usage_error(error)
            raise

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ClickException as error:
            self._report_usage_error(error)
            raise


# 顶层命令分组 (rich_help_panel), 顺序即面板展示顺序
PANEL_CONFIG = "配置与交互"
PANEL_RESOURCE = "资源管理"
PANEL_PIPELINE = "标注流水线"
PANEL_MONITOR = "监控"

app = typer.Typer(
    name="aap",
    cls=_ErrorReportingGroup,
    help=(
        "AI 标注平台命令行工具 —— 项目 / 数据集 / 预测 / 导出 / 训练与在线学习的脚本化入口。\n\n"
        "凭据二选一: 运行 [bold cyan]aap login[/] 写入配置, "
        "或设环境变量 [bold]AAP_BASE_URL[/] / [bold]AAP_API_KEY[/]。\n"
        "脚本 / CI 场景给任意命令加 [bold]--json[/] 输出裸 JSON (无装饰, 错误走 stderr)。"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=CONTEXT_SETTINGS,
    epilog=(
        "快速上手: [dim]aap login --url http://localhost:8000[/] → "
        "[dim]aap projects list[/] → [dim]aap tui[/]。\n\n"
        "每个子命令都支持 [bold]-h[/] / [bold]--help[/] 查看用法与可复制示例。"
    ),
)
app.command(name="capabilities", rich_help_panel=PANEL_CONFIG)(discovery.capabilities)
app.command(name="describe", rich_help_panel=PANEL_CONFIG)(discovery.describe)
app.command(
    name="login",
    rich_help_panel=PANEL_CONFIG,
    epilog=(
        "示例: [dim]aap login --url http://localhost:8000[/] (省略 --api-key 时交互式隐藏输入)"
    ),
)(login.login)
app.add_typer(projects.app, name="projects", rich_help_panel=PANEL_RESOURCE)
app.add_typer(templates.app, name="templates", rich_help_panel=PANEL_RESOURCE)
app.add_typer(video_tracker.app, name="video-tracker", rich_help_panel=PANEL_PIPELINE)
app.add_typer(video_review.app, name="video-review", rich_help_panel=PANEL_PIPELINE)
app.add_typer(videos.app, name="videos", rich_help_panel=PANEL_RESOURCE)
app.add_typer(videos.task_app, name="task-videos", rich_help_panel=PANEL_PIPELINE)
app.add_typer(files.app, name="files", rich_help_panel=PANEL_RESOURCE)
app.add_typer(scenes.app, name="scenes", rich_help_panel=PANEL_RESOURCE)
app.add_typer(health.app, name="health", rich_help_panel=PANEL_MONITOR)
app.add_typer(auth.app, name="auth", rich_help_panel=PANEL_CONFIG)
app.add_typer(
    ml_capabilities.app, name="ml-capabilities", rich_help_panel=PANEL_RESOURCE
)
app.add_typer(system_settings.app, name="system-settings", rich_help_panel=PANEL_CONFIG)
app.add_typer(account.app, name="account", rich_help_panel=PANEL_CONFIG)
app.add_typer(api_keys.app, name="api-keys", rich_help_panel=PANEL_CONFIG)
app.add_typer(users.app, name="users", rich_help_panel=PANEL_RESOURCE)
app.add_typer(groups.app, name="groups", rich_help_panel=PANEL_RESOURCE)
app.add_typer(invitations.app, name="invitations", rich_help_panel=PANEL_RESOURCE)
app.add_typer(
    storage_connections.app, name="storage-connections", rich_help_panel=PANEL_RESOURCE
)
app.add_typer(
    admin_operations.app, name="admin-operations", rich_help_panel=PANEL_MONITOR
)
app.add_typer(ml_diagnostics.app, name="ml-diagnostics", rich_help_panel=PANEL_MONITOR)
app.add_typer(project_ml.app, name="project-ml", rich_help_panel=PANEL_PIPELINE)
app.add_typer(audit_logs.app, name="audit-logs", rich_help_panel=PANEL_MONITOR)
app.add_typer(guide_assets.app, name="guide-assets", rich_help_panel=PANEL_RESOURCE)
app.add_typer(
    project_performance.app, name="project-performance", rich_help_panel=PANEL_MONITOR
)
app.add_typer(search.app, name="search", rich_help_panel=PANEL_RESOURCE)
app.add_typer(streams.app, name="streams", rich_help_panel=PANEL_MONITOR)
app.add_typer(task_context.app, name="task-context", rich_help_panel=PANEL_PIPELINE)
app.add_typer(image_pyramids.app, name="image-pyramids", rich_help_panel=PANEL_PIPELINE)
app.add_typer(
    cross_frame_jobs.app, name="cross-frame-jobs", rich_help_panel=PANEL_PIPELINE
)
app.add_typer(task_locks.app, name="task-locks", rich_help_panel=PANEL_PIPELINE)
app.add_typer(point_cloud.app, name="point-cloud", rich_help_panel=PANEL_PIPELINE)
app.add_typer(scene_tracks.app, name="scene-tracks", rich_help_panel=PANEL_PIPELINE)
app.add_typer(
    annotation_operations.app,
    name="annotation-operations",
    rich_help_panel=PANEL_PIPELINE,
)
app.add_typer(comments.app, name="comments", rich_help_panel=PANEL_RESOURCE)
app.add_typer(
    annotation_history.app, name="annotation-history", rich_help_panel=PANEL_RESOURCE
)
app.add_typer(bug_reports.app, name="bug-reports", rich_help_panel=PANEL_RESOURCE)
app.add_typer(mask_formats.app, name="mask-formats", rich_help_panel=PANEL_RESOURCE)
app.add_typer(mask_qc.app, name="mask-qc", rich_help_panel=PANEL_RESOURCE)
app.add_typer(
    point_cloud_quality.app, name="point-cloud-quality", rich_help_panel=PANEL_RESOURCE
)
app.add_typer(
    failed_predictions.app, name="failed-predictions", rich_help_panel=PANEL_MONITOR
)
app.add_typer(storage.app, name="storage", rich_help_panel=PANEL_MONITOR)
app.add_typer(datasets.app, name="datasets", rich_help_panel=PANEL_RESOURCE)
app.add_typer(batches.app, name="batches", rich_help_panel=PANEL_RESOURCE)
app.add_typer(members.app, name="members", rich_help_panel=PANEL_RESOURCE)
app.add_typer(notifications.app, name="notifications", rich_help_panel=PANEL_RESOURCE)
app.add_typer(feedbacks.app, name="feedbacks", rich_help_panel=PANEL_RESOURCE)
app.add_typer(data_manager.app, name="data-manager", rich_help_panel=PANEL_RESOURCE)
app.add_typer(task_views.app, name="task-views", rich_help_panel=PANEL_RESOURCE)
app.add_typer(tasks.app, name="tasks", rich_help_panel=PANEL_PIPELINE)
app.add_typer(annotations.app, name="annotations", rich_help_panel=PANEL_PIPELINE)
app.add_typer(predictions.app, name="predictions", rich_help_panel=PANEL_PIPELINE)
app.add_typer(pipelines.app, name="pipelines", rich_help_panel=PANEL_PIPELINE)
app.add_typer(jobs.app, name="jobs", rich_help_panel=PANEL_PIPELINE)
app.add_typer(export.app, name="export", rich_help_panel=PANEL_PIPELINE)
app.add_typer(ml_backends.app, name="ml-backends", rich_help_panel=PANEL_MONITOR)
app.add_typer(ml_registry.app, name="ml-registry", rich_help_panel=PANEL_MONITOR)
app.add_typer(plugins.app, name="plugins", rich_help_panel=PANEL_MONITOR)
app.add_typer(service_pools.app, name="service-pools", rich_help_panel=PANEL_MONITOR)
app.add_typer(dashboard.app, name="dashboard", rich_help_panel=PANEL_MONITOR)
app.add_typer(learning.app, name="learning", rich_help_panel=PANEL_PIPELINE)


@app.command(rich_help_panel=PANEL_CONFIG)
def me(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """显示当前认证主体 (用户 / 角色), 用于自检凭据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            principal = client.me()
    if json_output:
        print_json(principal.model_dump(mode="json"))
    else:
        console.print(
            f"[green]{principal.name}[/green] <{principal.email}> · role={principal.role}"
        )


@app.command(rich_help_panel=PANEL_MONITOR)
def stats(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """可见项目聚合统计 + 最近 12 周趋势 (文本 sparkline)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            s = client.projects.stats()
    if json_output:
        print_json(s.model_dump(mode="json"))
        return
    rate = s.ai_rate * 100 if s.ai_rate <= 1 else s.ai_rate
    console.print(
        f"总量 [b]{s.total_data}[/b] · 完成 [b]{s.completed}[/b] · "
        f"AI率 [b]{rate:.0f}%[/b] · 待审 [b]{s.pending_review}[/b]"
    )
    console.print(f"数据总量(12周): {sparkline(s.total_data_series)}")
    console.print(f"完成量(12周):   {sparkline(s.completed_series)}")
    console.print(f"AI率(12周):     {sparkline(s.ai_rate_series)}")
    console.print(f"待审(12周):     {sparkline(s.pending_review_series)}")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aap {__version__}@AAP{__aap_target_version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="显示版本号并退出",
    ),
    error_format: str = typer.Option(
        "text", "--error-format", help="错误输出格式: text 或 json"
    ),
) -> None:
    """AI 标注平台 CLI。"""
    if error_format not in {"text", "json"}:
        raise typer.BadParameter("--error-format must be text or json")


@app.command(rich_help_panel=PANEL_CONFIG)
def tui() -> None:
    """启动终端监控面板 (Projects / Datasets / Jobs / ML Backends / Training)。

    安装: [bold]pip install 'ai-annotation-sdk\\[tui]'[/]
    """
    try:
        from ai_annotation.tui.app import run  # type: ignore[import-not-found]
    except ImportError:
        typer.echo("TUI 依赖未安装, 请先: pip install 'ai-annotation-sdk[tui]'")
        raise typer.Exit(code=1) from None
    run()
