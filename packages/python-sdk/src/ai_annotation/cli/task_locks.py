"""Explicit task edit-lease commands; there is no background renewal."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
)

app = typer.Typer(
    help="Acquire, heartbeat, and release task edit leases.", no_args_is_help=True
)


@app.command("acquire")
def acquire(
    task_id: str = typer.Argument(..., help="Task UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Acquire a 300-second lease; the server may take over an assignee's old lock."""
    confirm_destructive(
        "Acquire the task's 300-second edit lease? The server may take over an old assignee lock.",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            lock = client.task_locks.acquire(task_id)
    data = lock.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("heartbeat")
def heartbeat(
    task_id: str = typer.Argument(..., help="Task UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Extend your current lease by one server-default 300-second interval."""
    confirm_destructive(
        f"Extend your task {task_id} lease by 300 seconds?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_locks.heartbeat(task_id)
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print(f"status={result.status}; lease interval=300s")


@app.command("release")
def release(
    task_id: str = typer.Argument(..., help="Task UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Release your own lease; the API does not report whether a row existed."""
    confirm_destructive(f"Release your task {task_id} edit lease?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.task_locks.release(task_id)
    data = {"task_id": task_id, "status": "release_request_completed"}
    if json_output:
        print_json(data)
    else:
        console.print(
            "Release request completed; the API does not report prior lease presence."
        )
