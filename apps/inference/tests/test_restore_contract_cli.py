from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from aap_inference import restore_cli
from aap_inference.restore_contract import (
    RESTORE_DATA_PROTOCOL,
    RESTORE_PROTOCOL,
    RestoreUnavailable,
    decode_native_envelope,
    decode_restore_command,
)


def _command() -> dict[str, str]:
    return {
        "schema": RESTORE_PROTOCOL,
        "restore_id": "11111111-1111-4111-8111-111111111111",
        "backup_sha256": "1" * 64,
        "fence_sha256": "2" * 64,
        "source_jobs_realm": "16384:source_db:1234567890123456789",
        "target_jobs_realm": "16384:target_db:1234567890123456789",
        "source_routing_realm": "16385:routing_source",
        "target_routing_realm": "16385:routing_target",
        "source_jobs_secret_sha256": "3" * 64,
        "source_routing_secret_sha256": "4" * 64,
    }


def _identity() -> dict[str, str]:
    return {
        "source_jobs_realm": "16384:source_db:1234567890123456789",
        "target_jobs_realm": "16384:target_db:1234567890123456789",
        "source_routing_realm": "16385:routing_source",
        "target_routing_realm": "16385:routing_target",
        "source_jobs_secret_sha256": "3" * 64,
        "source_routing_secret_sha256": "4" * 64,
    }


def _write_dsn(path: Path, *, mode: int = 0o600) -> str:
    value = "postgresql://operator:dsn-password-must-not-print@localhost/restore\n"
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return value.rstrip("\n")


@pytest.mark.parametrize(
    "mutate",
    [
        "duplicate",
        "nonfinite",
        "extra",
    ],
)
def test_restore_command_decoder_rejects_duplicate_nonfinite_and_extra_fields(mutate):
    command = _command()
    if mutate == "duplicate":
        raw = (
            json.dumps(command)
            .replace('"schema":', '"schema":"aap-video-restore/v1","schema":', 1)
            .encode("utf-8")
        )
        with pytest.raises(RestoreUnavailable):
            decode_restore_command(raw)
    elif mutate == "nonfinite":
        command["backup_sha256"] = float("nan")
        raw = json.dumps(command).encode("utf-8")
        with pytest.raises(RestoreUnavailable):
            decode_restore_command(raw)
    else:
        command["unexpected"] = "extra"
        with pytest.raises(RestoreUnavailable):
            decode_restore_command(command)


def test_native_rejection_reason_is_a_finite_closed_code_set():
    valid = {
        "schema": RESTORE_DATA_PROTOCOL,
        "status": "rejected",
        "reason": "authority_unavailable",
    }
    assert (
        decode_native_envelope(valid, action="inspect")["reason"]
        == "authority_unavailable"
    )

    with pytest.raises(RestoreUnavailable):
        decode_native_envelope(
            {**valid, "reason": "private_route_token_value"}, action="inspect"
        )


class _InspectProvider:
    def __init__(self, *, rejected: bool = False, extra_context: bool = False) -> None:
        self.rejected = rejected
        self.extra_context = extra_context
        self.private_context = {
            "endpoint": {"auth_token": "private-auth-token-must-not-print"},
            "signing_key": "private-signing-key-must-not-print",
        }

    async def step(self, action, arguments):
        assert action == "inspect"
        assert arguments == {}
        if self.rejected:
            return {
                "schema": RESTORE_DATA_PROTOCOL,
                "status": "rejected",
                "reason": "authority_unavailable",
            }
        response = {
            "schema": RESTORE_DATA_PROTOCOL,
            "status": "ok",
            "identity": _identity(),
        }
        if self.extra_context:
            response["contexts"] = [self.private_context]
        return response


@pytest.mark.asyncio
async def test_cli_inspect_success_and_failure_print_no_private_context(
    monkeypatch, tmp_path, capsys
):
    dsn_path = tmp_path / "database-url"
    _write_dsn(dsn_path)
    providers = iter(
        [
            _InspectProvider(),
            _InspectProvider(rejected=True),
            _InspectProvider(extra_context=True),
        ]
    )

    def provider_factory(_dsn):
        return next(providers)

    monkeypatch.setattr(restore_cli, "VideoRestorePostgresProvider", provider_factory)
    args = SimpleNamespace(
        database_url_file=str(dsn_path),
        inspect=True,
        status=None,
        command_file=None,
    )

    success_code = await restore_cli._run(args)
    success_output = capsys.readouterr().out
    failure_code = await restore_cli._run(args)
    failure_output = capsys.readouterr().out
    malformed_code = await restore_cli._run(args)
    malformed_output = capsys.readouterr().out

    assert success_code == 0
    assert json.loads(success_output) == _identity()
    assert failure_code == 2
    assert json.loads(failure_output) == {"reason": "authority_unavailable"}
    assert malformed_code == 2
    assert json.loads(malformed_output) == {"reason": "owner_unavailable"}
    for output in (success_output, failure_output, malformed_output):
        assert "private-auth-token-must-not-print" not in output
        assert "private-signing-key-must-not-print" not in output
        assert "dsn-password-must-not-print" not in output
        assert "contexts" not in output


def test_cli_reads_owner_private_dsn_and_valid_command_file(tmp_path):
    dsn_path = tmp_path / "database-url"
    expected_dsn = _write_dsn(dsn_path, mode=0o400)
    command_path = tmp_path / "restore-command.json"
    command_path.write_text(json.dumps(_command()), encoding="utf-8")
    command_path.chmod(0o600)

    assert restore_cli._database_url(str(dsn_path)) == expected_dsn
    assert restore_cli._command_file(str(command_path)).as_dict() == _command()


@pytest.mark.parametrize(
    "case", ["group_readable", "wrong_owner", "symlink", "oversized", "fifo"]
)
def test_cli_rejects_unsafe_database_url_files(monkeypatch, tmp_path, case):
    path = tmp_path / "database-url"
    if case == "group_readable":
        _write_dsn(path, mode=0o640)
    elif case == "wrong_owner":
        _write_dsn(path)
        owner = path.stat().st_uid
        monkeypatch.setattr(restore_cli.os, "geteuid", lambda: owner + 1)
    elif case == "symlink":
        target = tmp_path / "database-url-target"
        _write_dsn(target)
        path.symlink_to(target)
    elif case == "oversized":
        path.write_bytes(b"x" * 8193)
        path.chmod(0o600)
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("named pipes are not supported on this platform")
        os.mkfifo(path, 0o600)

    with pytest.raises(RestoreUnavailable):
        restore_cli._database_url(str(path))
