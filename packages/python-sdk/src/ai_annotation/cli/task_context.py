"""Read-only task context commands for annotations, neighbors, masks, and predictions."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import cli_errors, console, get_client, print_json

app = typer.Typer(
    help="Read paged task annotations, scene context, mask capabilities, and predictions.",
    no_args_is_help=True,
)


@app.command("annotations-page")
def annotations_page(
    task_id: str = typer.Argument(..., help="Task UUID"),
    limit: int = typer.Option(200, "--limit", min=1, max=1000, help="Page size"),
    cursor: str | None = typer.Option(None, "--cursor", help="Opaque next-page cursor"),
    video_segment_id: str | None = typer.Option(
        None, "--video-segment-id", help="Exact video collaboration segment UUID"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Read one saved-annotation page. Continue with the returned opaque cursor."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.task_context.annotations_page(
                task_id,
                limit=limit,
                cursor=cursor,
                video_segment_id=video_segment_id,
            )
    data = page.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("neighbors")
def neighbors(
    task_id: str = typer.Argument(..., help="Task UUID"),
    k: int = typer.Option(1, "--k", min=1, max=20, help="Neighbors per direction"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List visible scene-frame neighbors; non-scene tasks return empty directions."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_context.neighbors(task_id, k=k)
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("neighbor-annotations")
def neighbor_annotations(
    task_id: str = typer.Argument(..., help="Task UUID"),
    k: int = typer.Option(1, "--k", min=1, max=20, help="Neighbors per direction"),
    track_id: str | None = typer.Option(
        None, "--track-id", help="Filter to one cross-frame track"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Read visible neighbor frames and their server-filtered annotations."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_context.neighbor_annotations(
                task_id, k=k, track_id=track_id
            )
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("mask-capabilities")
def mask_capabilities(
    task_id: str = typer.Argument(..., help="Task UUID"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Read the server's current mask read/write limits and task-lock state."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_context.mask_capabilities(task_id)
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("predictions")
def predictions(
    task_id: str = typer.Argument(..., help="Task UUID"),
    model_version: str | None = typer.Option(None, "--model-version"),
    min_confidence: float | None = typer.Option(
        None, "--min-confidence", min=0.0, max=1.0
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, max=2000),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Read prediction shapes without renumbering their original shape indexes."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_context.predictions(
                task_id,
                model_version=model_version,
                min_confidence=min_confidence,
                limit=limit,
                offset=offset,
            )
    data = [item.model_dump(mode="json", by_alias=True) for item in result]
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)
