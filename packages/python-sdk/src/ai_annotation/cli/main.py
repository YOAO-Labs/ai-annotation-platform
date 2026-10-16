"""aap CLI 入口: 命令组装。

需要 cli extras: pip install 'ai-annotation-sdk[cli]'。
"""

from __future__ import annotations

import typer

from ai_annotation import __aap_target_version__, __version__
from ai_annotation.cli import (
    annotations,
    batches,
    dashboard,
    datasets,
    export,
    jobs,
    learning,
    login,
    members,
    ml_backends,
    ml_registry,
    predictions,
    projects,
    service_pools,
    tasks,
)
from ai_annotation.cli._output import (
    cli_errors,
    console,
    get_client,
    print_json,
    sparkline,
)

# 让 -h 等价 --help (root 设置经 Click context 继承到所有子命令)
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# 顶层命令分组 (rich_help_panel), 顺序即面板展示顺序
PANEL_CONFIG = "配置与交互"
PANEL_RESOURCE = "资源管理"
PANEL_PIPELINE = "标注流水线"
PANEL_MONITOR = "监控"

app = typer.Typer(
    name="aap",
    help=(
        "AI 标注平台命令行工具 —— 项目 / 数据集 / 预测 / 导出 / 异步任务的脚本化入口。\n\n"
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
app.command(
    name="login",
    rich_help_panel=PANEL_CONFIG,
    epilog=(
        "示例: [dim]aap login --url http://localhost:8000[/] (省略 --api-key 时交互式隐藏输入)"
    ),
)(login.login)
app.add_typer(projects.app, name="projects", rich_help_panel=PANEL_RESOURCE)
app.add_typer(datasets.app, name="datasets", rich_help_panel=PANEL_RESOURCE)
app.add_typer(batches.app, name="batches", rich_help_panel=PANEL_RESOURCE)
app.add_typer(members.app, name="members", rich_help_panel=PANEL_RESOURCE)
app.add_typer(tasks.app, name="tasks", rich_help_panel=PANEL_PIPELINE)
app.add_typer(annotations.app, name="annotations", rich_help_panel=PANEL_PIPELINE)
app.add_typer(predictions.app, name="predictions", rich_help_panel=PANEL_PIPELINE)
app.add_typer(jobs.app, name="jobs", rich_help_panel=PANEL_PIPELINE)
app.add_typer(export.app, name="export", rich_help_panel=PANEL_PIPELINE)
app.add_typer(ml_backends.app, name="ml-backends", rich_help_panel=PANEL_MONITOR)
app.add_typer(ml_registry.app, name="ml-registry", rich_help_panel=PANEL_MONITOR)
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
) -> None:
    """AI 标注平台 CLI。"""


@app.command(rich_help_panel=PANEL_CONFIG)
def tui() -> None:
    """启动终端监控面板 (Projects / Datasets / Jobs / ML Backends; 需要 tui extras)。

    安装: [bold]pip install 'ai-annotation-sdk\\[tui]'[/]
    """
    try:
        from ai_annotation.tui.app import run  # type: ignore[import-not-found]
    except ImportError:
        typer.echo("TUI 依赖未安装, 请先: pip install 'ai-annotation-sdk[tui]'")
        raise typer.Exit(code=1) from None
    run()
