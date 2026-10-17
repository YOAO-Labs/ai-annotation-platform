"""CLI for project ML backend configuration, inference and runtime controls."""

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.project_ml_models import (
    ProjectMLBackendCreateRequest,
    ProjectMLBackendReloadRequest,
    ProjectMLBackendUpdateRequest,
    ProjectMLFrameInteractiveRequest,
    ProjectMLFramePredictRequest,
    ProjectMLInteractiveRequest,
    ProjectMLWarmupRequest,
)

app = typer.Typer(
    help="项目范围的 ML backend 配置、推理与运行时控制。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("create-backend")
def create_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    body_file: Path = typer.Option(
        ..., "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认登记或启用全局 backend"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """登记或复用一个全局 registry backend，并在此项目启用。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, ProjectMLBackendCreateRequest)
        confirm_destructive("确认登记或启用该项目的 ML backend?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.create_backend(project_id, body)
    print_learning_result(result, json_output)


@app.command("update-backend")
def update_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path = typer.Option(
        ..., "--body-file", help="部分更新 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认修改共享全局 backend 配置"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """修改 registry backend 的共享属性；可能影响其他启用该 backend 的项目。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, ProjectMLBackendUpdateRequest)
        confirm_destructive("确认修改共享全局 ML backend 配置?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.update_backend(project_id, backend_id, body)
    print_learning_result(result, json_output)


@app.command("disable-backend")
def disable_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认在此项目停用 backend"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """停用本项目对 backend 的关联；不删除全局 registry 记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认在该项目停用 ML backend?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.disable_backend(project_id, backend_id)
    print_learning_result(result, json_output)


@app.command("setup")
def setup(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取 backend 原始 /setup schema；服务端缓存最长约 30 秒。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_ml.setup(project_id, backend_id)
    print_learning_result(result, json_output)


@app.command("capabilities")
def capabilities(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取服务端从 /setup 派生的多模型能力快照。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_ml.capabilities(project_id, backend_id)
    print_learning_result(result, json_output)


@app.command("refresh-capabilities")
def refresh_capabilities(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认清缓存并请求 backend /setup"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """清除 setup 缓存并强制重新探测远端 backend。"""
    with cli_errors(json_output):
        confirm_destructive("确认清除 setup 缓存并探测 backend?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.refresh_capabilities(project_id, backend_id)
    print_learning_result(result, json_output)


@app.command("interactive")
def interactive(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path = typer.Option(
        ..., "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认调用远端模型进行推理"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """运行交互式推理并返回暂存候选；结果不会在此端点写入 Annotation。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, ProjectMLInteractiveRequest)
        confirm_destructive("确认调用远端 backend 进行交互推理?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.interactive(project_id, backend_id, body)
    print_learning_result(result, json_output)


@app.command("interactive-frame")
def interactive_frame(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path = typer.Option(
        ...,
        "--body-file",
        help="含 frame_path/task_id/frame_index/context 的 JSON 文件",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认上传当前帧并调用远端模型"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传一帧 JPEG/PNG 并运行交互推理；候选只返回给调用方。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, ProjectMLFrameInteractiveRequest)
        confirm_destructive("确认上传帧图并调用远端 backend?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.interactive_frame(project_id, backend_id, body)
    print_learning_result(result, json_output)


@app.command("predict-frame")
def predict_frame(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path = typer.Option(
        ..., "--body-file", help="含 frame_path/task_id/frame_index/config 的 JSON 文件"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认推理并保存待采纳 Prediction"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """对上传帧执行图像预测，并在服务端创建待采纳 Prediction。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, ProjectMLFramePredictRequest)
        confirm_destructive("确认推理并创建待采纳 Prediction?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.predict_frame(project_id, backend_id, body)
    print_learning_result(result, json_output)


@app.command("predict-test")
def predict_test(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    task_id: str = typer.Argument(..., help="项目内任务 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认运行一次完整任务推理"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以完整 task media URL 运行一次推理，返回候选但不持久化 Prediction。"""
    with cli_errors(json_output):
        confirm_destructive("确认对完整任务调用远端模型推理?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.predict_test(project_id, backend_id, task_id)
    print_learning_result(result, json_output)


@app.command("reload")
def reload_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="可选变体 JSON 文件"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认修改共享 backend 驻留模型"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重新加载共享 backend 模型或选择变体；仅超管可调用。"""
    with cli_errors(json_output):
        body = (
            read_json_body(body_file, ProjectMLBackendReloadRequest)
            if body_file is not None
            else None
        )
        confirm_destructive("确认 reload 共享 backend 模型?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.reload(project_id, backend_id, body)
    print_learning_result(result, json_output)


@app.command("unload")
def unload_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认卸载共享 backend 驻留模型"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """在 server quiescence 守卫通过后卸载共享模型。"""
    with cli_errors(json_output):
        confirm_destructive("确认 unload 共享 backend 模型?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.unload(project_id, backend_id)
    print_learning_result(result, json_output)


@app.command("warmup")
def warmup_backend(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    backend_id: str = typer.Argument(..., help="全局 backend UUID"),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="backend 专属 JSON body，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认调用 backend warmup"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """调用 backend 专属 warmup；预热可能影响共享模型驻留。"""
    with cli_errors(json_output):
        body = (
            read_json_body(body_file, ProjectMLWarmupRequest)
            if body_file is not None
            else None
        )
        confirm_destructive("确认 warmup 共享 backend 模型?", yes, json_output)
        with get_client(json_output) as client:
            result = client.project_ml.warmup(project_id, backend_id, body)
    print_learning_result(result, json_output)
