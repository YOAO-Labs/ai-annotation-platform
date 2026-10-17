"""aap health commands return JSON reports and fail on degraded checks."""

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result
from ai_annotation.health_models import HealthCheck, HealthReport

app = typer.Typer(
    help="平台根健康检查；降级时输出报告并返回非零状态。", no_args_is_help=True
)


def _print_health(result: HealthCheck | HealthReport, json_output: bool) -> None:
    print_learning_result(result, json_output)
    if result.status != "ok":
        raise typer.Exit(1)


@app.command("get")
def get(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output, require_auth=False) as client:
            result = client.health.get()
    _print_health(result, json_output)


@app.command("db")
def db(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output, require_auth=False) as client:
            result = client.health.db()
    _print_health(result, json_output)


@app.command("redis")
def redis(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output, require_auth=False) as client:
            result = client.health.redis()
    _print_health(result, json_output)


@app.command("minio")
def minio(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output, require_auth=False) as client:
            result = client.health.minio()
    _print_health(result, json_output)


@app.command("celery")
def celery(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output, require_auth=False) as client:
            result = client.health.celery()
    _print_health(result, json_output)
