"""Explicit commands for current-user cross-frame propagation jobs."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    read_json_body,
)
from ai_annotation.task_workflow_models import CrossFrameJobCreateRequest

app = typer.Typer(
    help="Create, inspect, and retry cross-frame jobs.", no_args_is_help=True
)


@app.command("list")
def list_jobs(
    task_id: str = typer.Argument(..., help="Anchor task UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=50),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List this actor's cross-frame jobs in the anchor task's scene."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.cross_frame_jobs.list(task_id, limit=limit)
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("create")
def create(
    task_id: str = typer.Argument(..., help="Source task UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON request file or - for stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Queue a bounded job after server validation of visible editable targets."""
    with cli_errors(json_output):
        body = read_json_body(body_path, CrossFrameJobCreateRequest)
        confirm_destructive("Queue this cross-frame propagation job?", yes, json_output)
        with get_client(json_output) as client:
            job = client.cross_frame_jobs.create(task_id, body)
    data = job.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("retry-failed")
def retry_failed(
    task_id: str = typer.Argument(..., help="Anchor task UUID"),
    job_id: str = typer.Argument(..., help="Your terminal cross-frame job UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Retry only failed or stale frames eligible under the original job snapshot."""
    confirm_destructive(
        f"Retry eligible failed frames from {job_id}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            job = client.cross_frame_jobs.retry_failed(task_id, job_id)
    data = job.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)
