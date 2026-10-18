"""Image-pyramid manifest, signed-asset, and generation-retry commands."""

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
from ai_annotation.task_workflow_models import ImagePyramidAssetUrlsRequest

app = typer.Typer(
    help="Read image-pyramid assets and retry generation explicitly.",
    no_args_is_help=True,
)


@app.command("manifest")
def manifest(
    task_id: str = typer.Argument(..., help="Task UUID"),
    json_output: bool = typer.Option(
        False, "--json", help="Output raw JSON, including expiring signed URLs"
    ),
) -> None:
    """Read the current pyramid generation and short-lived overview URL."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.image_pyramids.manifest(task_id)
    if json_output:
        print_json(result.model_dump(mode="json", by_alias=True))
        return
    console.print(
        f"status={result.status} generation={result.generation} "
        f"building_generation={result.building_generation} required={result.required}"
    )
    if result.overview is not None:
        console.print(
            f"overview URL expires at {result.overview.expires_at.isoformat()}; "
            "use --json to expose the signed URL."
        )


@app.command("asset-urls")
def asset_urls(
    task_id: str = typer.Argument(..., help="Task UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON asset list or - for stdin"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output raw JSON, including expiring signed URLs"
    ),
) -> None:
    """Mint signed URLs for up to 128 assets from one exact generation."""
    with cli_errors(json_output):
        body = read_json_body(body_path, ImagePyramidAssetUrlsRequest)
        with get_client(json_output) as client:
            result = client.image_pyramids.asset_urls(task_id, body)
    if json_output:
        print_json(result.model_dump(mode="json", by_alias=True))
        return
    console.print(
        f"task_id={result.task_id} generation={result.generation} "
        f"expires_at={result.expires_at.isoformat()}"
    )
    for item in result.items:
        identity = [item.kind, f"generation={item.generation}"]
        if item.level is not None:
            identity.extend([f"level={item.level}", f"x={item.x}", f"y={item.y}"])
        console.print(" ".join(identity))
    console.print("Signed URLs are hidden in text output; use --json to expose them.")


@app.command("retry")
def retry(
    task_id: str = typer.Argument(..., help="Task UUID"),
    idempotency_key: str | None = typer.Option(
        None, "--idempotency-key", help="Reuse this UUID after a lost response"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Request one rate-limited generation retry; this is not an AsyncJob wait handle."""
    confirm_destructive(
        f"Request image-pyramid generation for task {task_id}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.image_pyramids.retry(
                task_id, idempotency_key=idempotency_key
            )
    data = result.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(data)
    else:
        console.print(
            f"status={result.status} job_id={result.job_id}; "
            "read the manifest to observe generation status."
        )
