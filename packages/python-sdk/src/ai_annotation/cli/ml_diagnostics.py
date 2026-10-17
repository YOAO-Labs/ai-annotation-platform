"""CLI for global ML backend diagnostics and runtime smoke tests."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.ml_diagnostic_models import ProbeRequest, SmokeTestRequest

app = typer.Typer(
    help="ML backend 运维诊断与运行时观测。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _read_token(env_name: str | None, prompt: bool) -> str | None:
    if env_name and prompt:
        raise typer.BadParameter("--auth-token-env 与 --prompt-auth-token 不能同时使用")
    if prompt:
        return typer.prompt("backend auth token", hide_input=True)
    if env_name:
        token = os.environ.get(env_name)
        if token is None:
            raise typer.BadParameter(f"环境变量 {env_name} 未设置")
        return token
    return None


@app.command("overview")
def overview(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认列举五个对象桶"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """汇总对象桶容量与全局项目/backend 绑定，可能扫描大量对象。"""
    with cli_errors(json_output):
        confirm_destructive(
            "确认扫描五个存储桶并读取全局 backend 概览?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.ml_diagnostics.overview()
    print_learning_result(result, json_output)


@app.command("gpu-resources")
def gpu_resources(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取 GPU 配额、滚动状态与 arbiter Redis 运行观测。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_diagnostics.gpu_resources()
    print_learning_result(result, json_output)


@app.command("observe")
def observe(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认探测已配置的 backend URL"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """探测环境配置的 backend /health 与 /setup。"""
    with cli_errors(json_output):
        confirm_destructive(
            "确认对配置的 backend URL 发起 health/setup 请求?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.ml_diagnostics.observe()
    print_learning_result(result, json_output)


@app.command("runtime-hints")
def runtime_hints(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """显示部署配置的默认 ML backend URL 提示。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_diagnostics.runtime_hints()
    print_learning_result(result, json_output)


@app.command("probe")
def probe(
    url: str = typer.Option(..., "--url", help="目标 backend base URL"),
    auth_method: str = typer.Option("none", "--auth-method", help="none 或 token"),
    auth_token_env: str | None = typer.Option(
        None, "--auth-token-env", help="从指定环境变量读取 token"
    ),
    prompt_auth_token: bool = typer.Option(
        False, "--prompt-auth-token", help="隐藏输入 backend token"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向 URL 发送 health 请求"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """向指定 URL 的 /health 发起网络探测；失败状态在 ProbeResponse.ok 中返回。"""
    if auth_method not in {"none", "token"}:
        raise typer.BadParameter("--auth-method 必须是 none 或 token")
    token = _read_token(auth_token_env, prompt_auth_token)
    with cli_errors(json_output):
        body = ProbeRequest(url=url, auth_method=auth_method, auth_token=token)
        confirm_destructive(
            "确认对指定 backend URL 发起 health 请求?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.ml_diagnostics.probe(body)
    print_learning_result(result, json_output)


@app.command("smoke-test")
def smoke_test(
    body_file: Path = typer.Option(
        ..., "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认可能 reload/unload backend 模型"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """在空闲池上尝试加载指定模型变体，并请求恢复卸载。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, SmokeTestRequest)
        confirm_destructive(
            "确认对指定 backend URL 执行 smoke test?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.ml_diagnostics.smoke_test(body)
    print_learning_result(result, json_output)
