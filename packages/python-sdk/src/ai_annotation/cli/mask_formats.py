"""aap mask-formats commands for explicit format and import-batch operations."""

from __future__ import annotations

import hashlib
import mimetypes
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
from ai_annotation.mask_format_models import (
    MaskFormatExportPreflightRequest,
    MaskFormatImportExecuteRequest,
    MaskFormatImportPreflightRequest,
    MaskFormatUploadInitRequest,
)

app = typer.Typer(
    help="Mask-format catalogue, export preflight, and staged annotation imports.",
    no_args_is_help=True,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@app.command("list")
def list_formats(
    project_id: str = typer.Argument(..., help="Project UUID"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List formats available for a project's media type."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            formats = client.mask_formats.list(project_id)
    if json_output:
        print_json([item.model_dump(mode="json", by_alias=True) for item in formats])
    else:
        for item in formats:
            console.print(
                f"{item.format_id}\t{item.label}\t"
                f"import={item.import_capability.supported}\t"
                f"export={item.export_capability.supported}"
            )


@app.command("export-preflight")
def export_preflight(
    project_id: str = typer.Argument(..., help="Project UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON request file or - for stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Validate a mask-format export request without starting an export."""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskFormatExportPreflightRequest)
        with get_client(json_output) as client:
            result = client.mask_formats.export_preflight(project_id, body)
    if json_output:
        print_json(result.model_dump(mode="json", by_alias=True))
    else:
        console.print_json(data=result.model_dump(mode="json", by_alias=True))


@app.command("upload")
def upload(
    project_id: str = typer.Argument(..., help="Project UUID"),
    file_path: Path = typer.Argument(
        ..., exists=True, file_okay=True, dir_okay=False, readable=True
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Stage one source file; this does not create or execute an import batch."""
    confirm_destructive(
        "Upload this file to project-scoped staging storage?", yes, json_output
    )
    with cli_errors(json_output):
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        digest = _file_sha256(file_path)
        with get_client(json_output) as client:
            init = client.mask_formats.upload_init(
                project_id,
                MaskFormatUploadInitRequest(
                    file_name=file_path.name,
                    content_type=content_type,
                ),
            )
            client.mask_formats.upload_bytes(init.upload_url, file_path, content_type)
    result = {
        "object_key": init.object_key,
        "staged_sha256": digest,
        "expires_in": init.expires_in,
        "uploaded": True,
    }
    if json_output:
        print_json(result)
    else:
        console.print(
            f"[green]Staged upload complete[/green] object_key={init.object_key} "
            f"sha256={digest} expires_in={init.expires_in}s"
        )


@app.command("import-preflight")
def import_preflight(
    project_id: str = typer.Argument(..., help="Project UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON request file or - for stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(
        False, "--json", help="Output raw JSON, including the receipt"
    ),
) -> None:
    """Create a plan and one-time receipt for a previously staged file."""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskFormatImportPreflightRequest)
        confirm_destructive(
            "Create a mask-format import preflight receipt?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.mask_formats.import_preflight(project_id, body)
    if json_output:
        print_json(result.model_dump(mode="json", by_alias=True))
    else:
        console.print(
            f"[green]Import preflight created[/green] import_id={result.import_id} "
            f"plan_digest={result.plan.plan_digest} "
            f"receipt_expires_at={result.receipt_expires_at.isoformat()}\n"
            "Receipt hidden in text output; use --json to capture it."
        )


@app.command("execute-import")
def execute_import(
    project_id: str = typer.Argument(..., help="Project UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON request file or - for stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Queue exactly one import attempt using its preflight receipt and digest."""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskFormatImportExecuteRequest)
        confirm_destructive("Queue this mask-format import?", yes, json_output)
        with get_client(json_output) as client:
            batch = client.mask_formats.execute_import(project_id, body)
    if json_output:
        print_json(batch.model_dump(mode="json", by_alias=True))
    else:
        console.print_json(data=batch.model_dump(mode="json", by_alias=True))


@app.command("get-import")
def get_import(
    project_id: str = typer.Argument(..., help="Project UUID"),
    import_id: str = typer.Argument(..., help="Import batch UUID"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Read the batch status and its partial per-item result receipts."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.mask_formats.get_import(project_id, import_id)
    if json_output:
        print_json(batch.model_dump(mode="json", by_alias=True))
    else:
        console.print_json(data=batch.model_dump(mode="json", by_alias=True))


@app.command("resume-import")
def resume_import(
    project_id: str = typer.Argument(..., help="Project UUID"),
    import_id: str = typer.Argument(..., help="Import batch UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Resume a failed or partial import batch once."""
    confirm_destructive(f"Resume mask-format import {import_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.mask_formats.resume_import(project_id, import_id)
    if json_output:
        print_json(batch.model_dump(mode="json", by_alias=True))
    else:
        console.print_json(data=batch.model_dump(mode="json", by_alias=True))
