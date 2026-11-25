"""Administrator-managed, receipt-backed local inference plugin operations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from yoao.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
    read_strict_json_object,
)
from yoao.errors import SDKError, InstallError

app = typer.Typer(
    help="Manage trusted local inference plugin installations.", no_args_is_help=True
)


def _deployment_module():
    try:
        from yoao import plugin_deployment
    except ModuleNotFoundError as exc:
        if exc.name != "yoao_inference_contract" and not (exc.name or "").startswith(
            "yoao_inference_contract."
        ):
            raise
        raise InstallError(
            "Plugin deployment dependencies are missing; install 'yoao-sdk[plugins]'"
        ) from exc
    return plugin_deployment


def _deployment(client, state_dir: Path):
    return _deployment_module().LocalPluginDeployment(
        client.plugins, client._http._origin, state_dir
    )


def _inspect_image(image: str):
    return _deployment_module().inspect_image(image)


def _resolve_local_plugin_config(spec):
    return _deployment_module().resolve_local_plugin_config(spec)


def _read_keyring_file(path: Path) -> dict[str, str]:
    data = read_strict_json_object(
        path, error_message="Unable to read GPU verification keyring JSON file"
    )
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in data.items()
    ):
        raise SDKError(
            "GPU verification keyring file must be a JSON object mapping key IDs to public keys"
        )
    return data


def _qualification_options(
    *,
    fixture: Path | None,
    asset_dir: Path | None,
    model_approval_ref: str | None,
    fixture_approval_ref: str | None,
):
    return _deployment_module().QualificationOptions(
        fixture=fixture,
        asset_dir=asset_dir,
        model_approval_ref=model_approval_ref,
        fixture_approval_ref=fixture_approval_ref,
    )


def _parse_install_qualification(
    *,
    qualification: Path | None,
    qualify: bool,
    fixture: Path | None,
    asset_dir: Path | None,
    model_approval_ref: str | None,
    fixture_approval_ref: str | None,
):
    has_options = any(
        value is not None
        for value in (fixture, asset_dir, model_approval_ref, fixture_approval_ref)
    )
    if qualify and qualification is not None:
        raise SDKError("Specify either --qualification or --qualify, not both")
    if not qualify and has_options:
        raise SDKError(
            "--fixture, --asset-dir, --model-approval-ref, and --fixture-approval-ref require --qualify"
        )
    if not qualify:
        return None
    return _qualification_options(
        fixture=fixture,
        asset_dir=asset_dir,
        model_approval_ref=model_approval_ref,
        fixture_approval_ref=fixture_approval_ref,
    )


@app.command("inspect-image")
def inspect_image_command(
    image: str = typer.Option(
        ..., "--image", help="Digest-pinned local plugin image reference"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect a digest-pinned local plugin image's OCI manifest and command labels."""
    with cli_errors(json_output):
        result = _inspect_image(image)
    print_learning_result(result, json_output)


@app.command("prepare")
def prepare(
    config: Path | None = typer.Option(
        None, "--config", help="Approved deployment JSON file"
    ),
    image: str | None = typer.Option(
        None, "--image", help="Digest-pinned local plugin image reference"
    ),
    name: str | None = typer.Option(
        None, "--name", help="Registry display name for this installation"
    ),
    url: str | None = typer.Option(
        None, "--url", help="Plugin HTTP(S) endpoint URL reachable by core"
    ),
    network: str | None = typer.Option(
        None, "--network", help="Explicit non-host Docker network"
    ),
    host_port: int | None = typer.Option(
        None,
        "--host-port",
        min=1,
        max=65535,
        help="Host port mapped to the plugin container",
    ),
    container_port: int | None = typer.Option(
        None,
        "--container-port",
        min=1,
        max=65535,
        help="Container port (defaults from entrypoint --port or 8000)",
    ),
    cpus: float | None = typer.Option(
        None, "--cpus", help="CPU quota for the plugin container"
    ),
    image_host: list[str] | None = typer.Option(
        None, "--image-host", help="Approved image fetch hostname (repeatable)"
    ),
    gpu_resource_id: str | None = typer.Option(
        None, "--gpu-resource-id", help="Core GPU resource identifier"
    ),
    physical_gpu_uuid: str | None = typer.Option(
        None, "--physical-gpu-uuid", help="Physical NVIDIA GPU UUID"
    ),
    gpu_verify_keyring: Path | None = typer.Option(
        None,
        "--gpu-verify-keyring",
        help="JSON file mapping key_id to Ed25519 public key",
    ),
    state_dir: Path = typer.Option(
        ..., "--state-dir", help="Private per-installation state directory"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Approve manifest resolved from OCI image label",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reserve an inactive registry and resource binding; no container is started."""
    with cli_errors(json_output):
        module = _deployment_module()
        inline_flags = (
            image,
            name,
            url,
            network,
            host_port,
            container_port,
            cpus,
            image_host,
            gpu_resource_id,
            physical_gpu_uuid,
            gpu_verify_keyring,
        )
        has_inline = any(value is not None for value in inline_flags)
        if config is not None and has_inline:
            raise SDKError(
                "Do not combine --config with --image or inline deployment flags"
            )
        if config is None and image is None:
            raise SDKError("Provide either --config or --image with deployment flags")
        if config is not None:
            deployment_input = read_json_body(config, module.LocalPluginDeploymentInput)
        else:
            missing = [
                flag
                for flag, value in (
                    ("--name", name),
                    ("--url", url),
                    ("--network", network),
                    ("--host-port", host_port),
                    ("--cpus", cpus),
                    ("--image-host", image_host),
                )
                if value is None or (isinstance(value, list) and not value)
            ]
            if missing:
                raise SDKError(
                    f"--image requires deployment flags: {', '.join(missing)}"
                )
            keyring = (
                _read_keyring_file(gpu_verify_keyring)
                if gpu_verify_keyring is not None
                else {}
            )
            deployment_input = module.LocalPluginDeploymentInput(
                name=name,
                url=url,
                image=image,
                gpu_resource_id=gpu_resource_id,
                physical_gpu_uuid=physical_gpu_uuid,
                network=network,
                host_port=host_port,
                container_port=container_port,
                cpus=cpus,
                gpu_verify_keyring=keyring,
                image_hosts=image_host,
            )
        body, from_oci_label = _resolve_local_plugin_config(deployment_input)
        if from_oci_label:
            confirm_destructive(
                f"Approve manifest for {body.manifest.plugin_id} {body.manifest.version} "
                f"from {body.manifest.artifact} and reserve this installation?",
                yes,
                json_output,
            )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).prepare(body)
    print_learning_result(result, json_output)


@app.command("qualify")
def qualify(
    state_dir: Path = typer.Option(
        ..., "--state-dir", help="Private per-installation state directory"
    ),
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Optional host fixture file mounted read-only"
    ),
    asset_dir: Path | None = typer.Option(
        None, "--asset-dir", help="Optional host asset directory mounted read-only"
    ),
    model_approval_ref: str | None = typer.Option(
        None, "--model-approval-ref", help="Approval reference for model weights"
    ),
    fixture_approval_ref: str | None = typer.Option(
        None, "--fixture-approval-ref", help="Approval reference for fixture"
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write verified report to this path without updating state-dir",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the image's OCI-declared GPU lifecycle qualification and verify the report."""
    with cli_errors(json_output):
        options = _qualification_options(
            fixture=fixture,
            asset_dir=asset_dir,
            model_approval_ref=model_approval_ref,
            fixture_approval_ref=fixture_approval_ref,
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).qualify(options, output_path=out)
    print_learning_result(result, json_output)


@app.command("prepare-assets")
def prepare_assets(
    state_dir: Path = typer.Option(..., "--state-dir"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explicitly prepare assets using the image's OCI prepare-assets command."""
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
    qualify: bool = typer.Option(
        False,
        "--qualify",
        help="Generate and verify GPU lifecycle qualification using the image's OCI command",
    ),
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Optional host fixture file for --qualify"
    ),
    asset_dir: Path | None = typer.Option(
        None, "--asset-dir", help="Optional host asset directory for --qualify"
    ),
    model_approval_ref: str | None = typer.Option(
        None,
        "--model-approval-ref",
        help="Approval reference for model weights during --qualify",
    ),
    fixture_approval_ref: str | None = typer.Option(
        None,
        "--fixture-approval-ref",
        help="Approval reference for fixture during --qualify",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Approve submission of lifecycle qualification"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start the owned image and activate only after core verifies the endpoint."""
    with cli_errors(json_output):
        qual_options = _parse_install_qualification(
            qualification=qualification,
            qualify=qualify,
            fixture=fixture,
            asset_dir=asset_dir,
            model_approval_ref=model_approval_ref,
            fixture_approval_ref=fixture_approval_ref,
        )
        confirm_destructive(
            "Activate this plugin and approve any supplied GPU lifecycle report?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = _deployment(client, state_dir).install(
                qualification,
                qualify=qualify,
                qualification_options=qual_options,
            )
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
        _deployment_module()
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
        _deployment_module()
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
    qualify: bool = typer.Option(
        False,
        "--qualify",
        help="Generate and verify GPU lifecycle qualification using the image's OCI command",
    ),
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Optional host fixture file for --qualify"
    ),
    asset_dir: Path | None = typer.Option(
        None, "--asset-dir", help="Optional host asset directory for --qualify"
    ),
    model_approval_ref: str | None = typer.Option(
        None,
        "--model-approval-ref",
        help="Approval reference for model weights during --qualify",
    ),
    fixture_approval_ref: str | None = typer.Option(
        None,
        "--fixture-approval-ref",
        help="Approval reference for fixture during --qualify",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Activate a separately prepared new identity; leave the old identity intact."""
    with cli_errors(json_output):
        qual_options = _parse_install_qualification(
            qualification=qualification,
            qualify=qualify,
            fixture=fixture,
            asset_dir=asset_dir,
            model_approval_ref=model_approval_ref,
            fixture_approval_ref=fixture_approval_ref,
        )
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
            result = new.install(
                qualification,
                qualify=qualify,
                qualification_options=qual_options,
            )
    print_learning_result(result, json_output)


@app.command("register-direct")
def register_direct(
    name: str = typer.Option(..., "--name", help="Registry display name"),
    url: str = typer.Option(..., "--url", help="Reachable HTTP(S) base URL"),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="Path to file containing the plugin bearer token (>=32 chars)",
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        envvar="YOAO_DIRECT_PLUGIN_TOKEN",
        help="Plugin bearer token (>=32 chars); prefer --token-file or env var",
    ),
    source_tier: str = typer.Option(
        "third_party",
        "--source-tier",
        help="Plugin governance tier: official or third_party",
    ),
    display_name: str | None = typer.Option(None, "--display-name"),
    description: str | None = typer.Option(None, "--description"),
    homepage: str | None = typer.Option(None, "--homepage"),
    publisher: str | None = typer.Option(None, "--publisher"),
    license_name: str | None = typer.Option(None, "--license"),
    asset_manifest_sha256: str | None = typer.Option(
        None,
        "--asset-manifest-sha256",
        help="Optional SHA-256 asset manifest digest to enforce asset approval",
    ),
    approved_compat_file: Path | None = typer.Option(
        None,
        "--approved-compat-file",
        help="JSON array file of approved ManagedModelCompatibility objects",
    ),
    approved_managed_evaluation: bool = typer.Option(
        False,
        "--approve-managed-evaluation",
        help="Approve managed evaluation when advertised by the plugin",
    ),
    installation_id: str | None = typer.Option(None, "--installation-id"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Register, probe, and activate a D1 unarbitrated direct inference plugin."""
    with cli_errors(json_output):
        from uuid import UUID
        from yoao_inference_contract.plugin import (
            ManagedModelCompatibility,
            RegisterDirectPluginRequest,
        )

        if token_file is not None and auth_token is not None:
            raise SDKError("Pass either --token-file or --auth-token, not both")
        if token_file is not None:
            try:
                resolved_token = token_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise SDKError(f"Unable to read --token-file: {exc}") from exc
        elif auth_token is not None:
            resolved_token = auth_token.strip()
        else:
            raise SDKError("Direct registration requires --token-file or --auth-token")
        approved_compat: list[ManagedModelCompatibility] = []
        if approved_compat_file is not None:
            try:
                raw_compat = json.loads(
                    approved_compat_file.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SDKError(
                    f"Unable to parse --approved-compat-file: {exc}"
                ) from exc
            if not isinstance(raw_compat, list):
                raise SDKError("--approved-compat-file must contain a JSON array")
            approved_compat = [
                ManagedModelCompatibility.model_validate(item) for item in raw_compat
            ]
        try:
            parsed_installation_id = (
                UUID(installation_id) if installation_id is not None else None
            )
        except ValueError as exc:
            raise SDKError(f"Invalid --installation-id: {exc}") from exc
        request = RegisterDirectPluginRequest(
            installation_id=parsed_installation_id,
            name=name,
            url=url,
            auth_token=resolved_token,
            source_tier=source_tier,
            display_name=display_name,
            description=description,
            homepage=homepage,
            publisher=publisher,
            license=license_name,
            asset_manifest_sha256=asset_manifest_sha256,
            approved_managed_model_compatibility=approved_compat,
            approved_managed_evaluation=approved_managed_evaluation,
        )
        confirm_destructive(
            "Register and activate this direct inference plugin?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.plugins.register_direct(request)
    print_learning_result(result, json_output)


@app.command("probe")
def probe(
    installation_id: str = typer.Option(
        ..., "--installation-id", help="Installed plugin installation UUID"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a read-only live probe against an installed plugin."""
    with cli_errors(json_output):
        from uuid import UUID

        try:
            parsed_id = UUID(installation_id)
        except ValueError as exc:
            raise SDKError(f"Invalid --installation-id: {exc}") from exc
        with get_client(json_output) as client:
            result = client.plugins.probe(parsed_id)
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
