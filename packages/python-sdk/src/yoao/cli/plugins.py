"""Administrator-managed, receipt-backed local inference plugin operations."""

from __future__ import annotations

from pathlib import Path

import typer

from yoao.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from yoao.errors import SDKError, InstallError

app = typer.Typer(
    help="Manage trusted local inference plugin installations.", no_args_is_help=True
)


def _deployment_types():
    try:
        from yoao.plugin_deployment import (
            LocalPluginConfig,
            LocalPluginDeployment,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "yoao_inference_contract":
            raise
        raise InstallError(
            "Plugin deployment dependencies are missing; install 'yoao-sdk[plugins]'"
        ) from exc
    return LocalPluginConfig, LocalPluginDeployment


def _deployment(client, state_dir: Path):
    return _deployment_types()[1](client.plugins, client._http._origin, state_dir)


@app.command("prepare")
def prepare(
    config: Path = typer.Option(..., "--config", help="Approved deployment JSON file"),
    state_dir: Path = typer.Option(
        ..., "--state-dir", help="Private per-installation state directory"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reserve an inactive registry and resource binding; no container is started."""
    with cli_errors(json_output):
        body = read_json_body(config, _deployment_types()[0])
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).prepare(body)
    print_learning_result(result, json_output)


@app.command("prepare-assets")
def prepare_assets(
    state_dir: Path = typer.Option(..., "--state-dir"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explicitly prepare the pinned default asset using the approved image."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).prepare_assets()
    print_learning_result(result, json_output)


@app.command("install")
def install(
    state_dir: Path = typer.Option(..., "--state-dir"),
    qualification: Path | None = typer.Option(
        None, "--qualification", help="Admin-approved GPU lifecycle report JSON"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Approve submission of lifecycle qualification"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start the owned image and activate only after core verifies the endpoint."""
    with cli_errors(json_output):
        confirm_destructive(
            "Activate this plugin and approve any supplied GPU lifecycle report?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).install(qualification)
    print_learning_result(result, json_output)


@app.command("migrate-state")
def migrate_state(
    state_dir: Path = typer.Option(..., "--state-dir"),
    manifest: Path = typer.Option(
        ..., "--manifest", help="Approved migrated manifest JSON"
    ),
    image_id: str = typer.Option(
        ..., "--image-id", help="Approved immutable local image ID"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Update a legacy receipt only after Registry and local ownership checks."""
    with cli_errors(json_output):
        _deployment_types()
        from yoao_inference_contract.plugin import PluginManifest

        body = read_json_body(manifest, PluginManifest)
        confirm_destructive("Migrate this local plugin receipt?", yes, json_output)
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).migrate_state(body, image_id)
    print_learning_result(result, json_output)


@app.command("asset-approvals")
def asset_approvals(
    state_dir: Path = typer.Option(..., "--state-dir"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read the current decision revision for an owned installation."""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            deployment = _deployment(client, state_dir)
            installation = deployment.status()
            result = client.plugins.asset_approval(
                installation.installation.installation_id
            )
    print_learning_result(result, json_output)


@app.command("approve-assets")
def approve_assets(
    state_dir: Path = typer.Option(..., "--state-dir"),
    descriptor: Path = typer.Option(
        ..., "--descriptor", help="Plugin-produced selection descriptor JSON"
    ),
    choices: Path = typer.Option(
        ..., "--choices", help="JSON object containing explicitly approved selections"
    ),
    expected_revision: int = typer.Option(..., "--expected-revision", min=0),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Replace decisions and measure available bytes for the owned active installation."""
    with cli_errors(json_output):
        _deployment_types()
        from yoao_inference_contract.plugin_assets import (
            PluginAssetChoices,
            PluginAssetDescriptor,
        )

        approved_descriptor = read_json_body(descriptor, PluginAssetDescriptor)
        approved_choices = read_json_body(choices, PluginAssetChoices)
        confirm_destructive(
            "Replace this installation's model asset approvals?", yes, json_output
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).approve_assets(
                approved_descriptor, approved_choices.approved, expected_revision
            )
    print_learning_result(result, json_output)


@app.command("status")
def status(
    state_dir: Path = typer.Option(..., "--state-dir"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).status()
    print_learning_result(result, json_output)


@app.command("disable")
def disable(
    state_dir: Path = typer.Option(..., "--state-dir"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Close core admission, prove quiescence, then stop the owned container."""
    with cli_errors(json_output):
        confirm_destructive(
            "Disable and drain this plugin installation?", yes, json_output
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).disable()
    print_learning_result(result, json_output)


@app.command("restart")
def restart(
    state_dir: Path = typer.Option(..., "--state-dir"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Drain and replace the owned process using its existing qualification."""
    with cli_errors(json_output):
        confirm_destructive(
            "Drain and restart this plugin installation?", yes, json_output
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).restart()
    print_learning_result(result, json_output)


@app.command("upgrade")
def upgrade(
    from_state: Path = typer.Option(
        ..., "--from-state", help="Existing installation receipt"
    ),
    state_dir: Path = typer.Option(
        ..., "--state-dir", help="New, prepared installation receipt"
    ),
    qualification: Path | None = typer.Option(
        None, "--qualification", help="Admin-approved GPU lifecycle report JSON"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Activate a separately prepared new identity; leave the old identity intact."""
    with cli_errors(json_output):
        confirm_destructive(
            "Activate a new plugin identity for upgrade?", yes, json_output
        )
        with get_client(json_output) as client:
            old = _deployment(client, from_state).status()
            new = _deployment(client, state_dir)
            candidate = new.status()
            if (
                old.installation.installation_id
                == candidate.installation.installation_id
                or old.registry_id == candidate.registry_id
            ):
                raise SDKError(
                    "Upgrade requires a new installation and registry identity"
                )
            if (
                old.installation.manifest.artifact
                == candidate.installation.manifest.artifact
            ):
                raise SDKError("Upgrade requires a new artifact digest")
            if old.installation.status != "active":
                raise SDKError(
                    "The existing installation must remain active during upgrade"
                )
            result = new.install(qualification)
    print_learning_result(result, json_output)


@app.command("uninstall")
def uninstall(
    state_dir: Path = typer.Option(..., "--state-dir"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Retire only after core dependency and quiescence checks; retain history."""
    with cli_errors(json_output):
        confirm_destructive("Retire this plugin installation?", yes, json_output)
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).uninstall()
    print_learning_result(result, json_output)
