"""ML capability discovery, distinct from installed offline SDK capabilities."""

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result

app = typer.Typer(help="读取已认证平台的协议词表与实例能力。", no_args_is_help=True)


@app.command("protocol")
def protocol(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_capabilities.protocol()
    print_learning_result(result, json_output)


@app.command("instances")
def instances(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_capabilities.instances()
    print_learning_result(result, json_output)
