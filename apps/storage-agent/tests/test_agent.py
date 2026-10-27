from copy import deepcopy
import json
import os
import time
from uuid import uuid4

import pytest

from aap_site_agent_contract import digest, validate_command
from aap_filesystem_read import capture_anchor
from aap_filesystem_read import roots as fs

from storage_agent.config import AgentError, Config, Credentials
from storage_agent.journal import Journal
from storage_agent.runner import Runner

from conftest import private_write


def acknowledgement(result, state="applied"):
    return {
        "protocol_version": 1,
        "command_id": result["command_id"],
        "command_digest": result["command_digest"],
        "result_digest": digest(result),
        "state": state,
        "code": "lease_expired" if state == "rejected" else None,
    }


def test_read_stage_is_private_complete_and_only_removed_after_business_ack(
    agent, command
):
    _, _, journal, runner, source = agent
    result = runner.execute(command)
    assert result["status"] == "completed" and result["closed"] is True
    with runner.outgoing(result) as body:
        assert body.read() == (source / "image.bin").read_bytes()
    path = journal.stages / (command["command_id"] + ".bin")
    assert path.stat().st_mode & 0o777 == 0o600
    journal.acknowledge(acknowledgement(result, "received"))
    assert path.exists() and journal.get(command["command_id"])["state"] == "received"
    journal.acknowledge(acknowledgement(result))
    assert not path.exists()
    assert journal.get(command["command_id"])["ack"] == acknowledgement(result)
    with pytest.raises(AgentError, match="command_already_acknowledged"):
        runner.execute(command)


def test_read_source_subdirectory_keeps_location_anchor(agent, command):
    config, _, _, runner, source = agent
    child = source / "dataset"
    child.mkdir()
    (child / "image.bin").write_bytes(b"nested source pixels")
    info = child.stat()
    command["arguments"].update(
        source_path="dataset",
        expected_anchor=capture_anchor(config.roots["images"]),
        expected_source_identity=[info.st_dev, info.st_ino],
    )
    result = runner.execute(command)
    assert result["status"] == "completed", result
    assert result["result"]["observation"]["source_identity"] == [
        info.st_dev,
        info.st_ino,
    ]
    with runner.outgoing(result) as body:
        assert body.read() == b"nested source pixels"


def test_restart_replays_exact_result_without_rereading_changed_source(agent, command):
    config, credentials, journal, runner, source = agent
    result = runner.execute(command)
    journal.close()
    (source / "image.bin").write_bytes(b"changed after completed read")
    with Journal(config.state_dir) as reopened:
        restarted = Runner(config, credentials, reopened)
        assert restarted.execute(command) == result
        with restarted.outgoing(result) as body:
            assert body.read() == b"source pixels"
        reopened.acknowledge(acknowledgement(result))


def test_interrupted_accepted_command_is_executed_after_restart(agent, command):
    config, credentials, journal, _, _ = agent
    journal.accept(validate_command(command))
    journal.close()
    with Journal(config.state_dir) as reopened:
        result = Runner(config, credentials, reopened).execute(command)
        assert result["status"] == "completed"


def test_poll_session_survives_restart_and_changes_with_identity(agent):
    config, credentials, journal, _, _ = agent
    session = journal.poll_session(credentials)
    journal.close()
    with Journal(config.state_dir) as reopened:
        assert reopened.poll_session(credentials) == session
        revised = Credentials(credentials.agent_id, 2, "t" * 40, credentials.expires_at)
        assert reopened.poll_session(revised) != session


def test_command_and_result_conflicts_never_replace_persisted_truth(agent, command):
    _, _, journal, runner, _ = agent
    result = runner.execute(command)
    conflicting = deepcopy(command)
    conflicting["arguments"]["path"] = "another.bin"
    with pytest.raises(AgentError, match="command_replay_conflict"):
        runner.execute(conflicting)
    other = deepcopy(result)
    other["result"]["size_bytes"] += 1
    with pytest.raises(AgentError, match="result_replay_conflict"):
        journal.complete(other)
    assert journal.get(command["command_id"])["result"] == result


@pytest.mark.parametrize("field", ["command_id", "command_digest", "result_digest"])
def test_forged_ack_keeps_stage_and_result(agent, command, field):
    _, _, journal, runner, _ = agent
    result = runner.execute(command)
    ack = acknowledgement(result)
    ack[field] = str(uuid4()) if field == "command_id" else "0" * 64
    with pytest.raises(AgentError, match="ack_identity_mismatch"):
        journal.acknowledge(ack)
    assert journal.get(command["command_id"])["state"] == "completed"
    assert list(journal.stages.iterdir())


def test_ack_cannot_regress_from_applied_to_received(agent, command):
    _, _, journal, runner, _ = agent
    result = runner.execute(command)
    journal.acknowledge(acknowledgement(result))
    journal.acknowledge(acknowledgement(result))
    with pytest.raises(AgentError, match="ack_state_regression"):
        journal.acknowledge(acknowledgement(result, "received"))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"root_revision": 2}, "root_revision_changed"),
        ({"root_handle": "unknown"}, "root_not_configured"),
        ({"issued_at": 1, "expires_at": 61}, "command_expired"),
    ],
)
def test_stale_root_and_expired_grant_do_not_read(agent, command, change, code):
    _, _, journal, runner, _ = agent
    command.update(change)
    result = runner.execute(command)
    assert result["status"] == "failed" and result["error_code"] == code
    assert not list(journal.stages.iterdir())


def test_live_authorized_read_with_missing_root_fails_without_outside_bytes(
    agent, command, monkeypatch
):
    _, credentials, journal, runner, source = agent
    assert command["issued_at"] <= time.time() < command["expires_at"]
    assert command["expires_at"] < credentials.expires_at
    (source / "image.bin").unlink()
    source.rmdir()
    outside = source.parent / "image.bin"
    outside.write_bytes(b"outside the configured root")
    opens = []
    original_open = fs._open_beneath

    def record_open(parent, path, **kwargs):
        opens.append(path)
        return original_open(parent, path, **kwargs)

    monkeypatch.setattr(fs, "_open_beneath", record_open)
    result = runner.execute(command)
    assert result["status"] == "failed"
    assert result["error_code"] == "path_unavailable"
    assert result["result"] == {} and result["closed"] is True
    assert time.time() < command["expires_at"]
    assert opens == [str(source).lstrip("/")]
    assert journal.get(command["command_id"])["state"] == "completed"
    assert runner.outgoing(result) is None
    assert not list(journal.stages.iterdir())
    assert outside.read_bytes() == b"outside the configured root"


def test_other_identity_has_no_local_checkpoint(agent, command):
    _, _, journal, runner, _ = agent
    command["agent_id"] = str(uuid4())
    with pytest.raises(AgentError, match="agent_identity_mismatch"):
        runner.execute(command)
    assert journal.pending() == []


def test_probe_scan_and_browse_execute_controlled_subtree(agent, command):
    _, _, _, runner, source = agent
    (source / "child").mkdir()
    for operation, arguments in [
        ("probe", {"sample_file": "image.bin"}),
        ("scan", {"path": ""}),
        ("browse", {"path": "", "limit": 20}),
    ]:
        candidate = deepcopy(command)
        candidate.update(command_id=str(uuid4()), operation=operation)
        candidate["arguments"].pop("path")
        candidate["arguments"].update(arguments)
        result = runner.execute(candidate)
        assert result["status"] == "completed"
        if operation == "probe":
            assert result["result"]["capabilities"]["read"] == "verified"
        elif operation == "scan":
            assert [entry["name"] for entry in result["result"]["entries"]] == [
                "child",
                "image.bin",
            ]
        else:
            assert result["result"]["names"] == ["child"]


def test_corrupted_persisted_stage_is_never_uploaded(agent, command):
    _, _, journal, runner, _ = agent
    result = runner.execute(command)
    (journal.stages / (command["command_id"] + ".bin")).write_bytes(b"corrupted")
    with pytest.raises(AgentError, match="stage_content_mismatch"):
        runner.outgoing(result)


def test_replaced_stage_special_file_does_not_block_or_send_bytes(agent, command):
    _, _, journal, runner, _ = agent
    result = runner.execute(command)
    path = journal.stages / (command["command_id"] + ".bin")
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(AgentError, match="stage_content_mismatch"):
        runner.outgoing(result)


def test_expired_credentials_and_future_clock_never_open_source(agent, command):
    config, credentials, journal, _, _ = agent
    from dataclasses import replace

    expired = replace(credentials, expires_at=1)
    result = Runner(config, expired, journal).execute(command)
    assert result["error_code"] == "credential_expired"
    other = deepcopy(command)
    other["command_id"] = str(uuid4())
    result = Runner(
        config, credentials, journal, clock=lambda: command["issued_at"] - 10
    ).execute(other)
    assert result["error_code"] == "clock_out_of_sync"
    assert not list(journal.stages.iterdir())


def test_journal_pending_limit_and_singleton(agent, command):
    config, _, journal, _, _ = agent
    journal.max_pending = 1
    journal.accept(validate_command(command))
    other = deepcopy(command)
    other["command_id"] = str(uuid4())
    with pytest.raises(AgentError, match="journal_capacity_exceeded"):
        journal.accept(validate_command(other))
    with pytest.raises(BlockingIOError):
        with Journal(config.state_dir):
            pass


def test_unrecorded_stages_are_removed_without_touching_pending_bytes(agent, command):
    _, _, journal, runner, _ = agent
    runner.execute(command)
    orphan = journal.stages / "orphan.partial"
    orphan.write_bytes(b"old")
    os.chmod(orphan, 0o600)
    journal.remove_orphans()
    assert not orphan.exists()
    assert len(list(journal.stages.iterdir())) == 1


def test_state_capacity_is_checked_before_source_stage(agent, command):
    config, credentials, journal, _, _ = agent
    from dataclasses import replace

    limited = replace(config, max_state_bytes=64)
    result = Runner(limited, credentials, journal).execute(command)
    assert result["error_code"] == "stage_capacity_exceeded"
    assert not list(journal.stages.iterdir())


def test_oversized_scan_result_is_a_durable_failure_instead_of_restart_loop(
    agent, command, monkeypatch
):
    _, _, journal, runner, _ = agent
    calls = []

    def large_manifest(*_args, **_kwargs):
        calls.append(True)
        return {"entries": [{"name": "图" * 80} for _ in range(5000)]}

    monkeypatch.setattr("storage_agent.runner.scan_directory", large_manifest)
    command["operation"] = "scan"
    command["arguments"]["path"] = ""
    result = runner.execute(command)
    assert result["status"] == "failed" and result["error_code"] == "message_too_large"
    assert result["closed"]
    assert runner.execute(command) == result
    assert len(calls) == 1
    assert journal.get(command["command_id"])["result"] == result


def config_value(tmp_path):
    return {
        "center_url": "https://center.example",
        "state_dir": str(tmp_path / "state"),
        "credential_file": str(tmp_path / "credentials.json"),
        "roots": {"images": {"path": str(tmp_path / "source"), "revision": 1}},
    }


@pytest.mark.parametrize(
    "origin",
    [
        "http://center.example",
        "https://u:p@center.example",
        "https://center.example/extra",
        "https://center.example?a=b",
        "https://center.example/#x",
        "https://center.example\n",
    ],
)
def test_only_fixed_https_origin_is_allowed(tmp_path, origin):
    value = config_value(tmp_path)
    value["center_url"] = origin
    path = tmp_path / "agent.json"
    private_write(path, value)
    with pytest.raises(AgentError, match="https_origin_required"):
        Config.load(path)


def test_local_config_ownership_permissions_and_overlap(tmp_path):
    value = config_value(tmp_path)
    path = tmp_path / "agent.json"
    private_write(path, value)
    assert sorted(Config.load(path).roots) == ["images"]
    os.chmod(path, 0o644)
    with pytest.raises(AgentError, match="private_config_file_required"):
        Config.load(path)
    value["state_dir"] = value["roots"]["images"]["path"] + "/state"
    private_write(path, value)
    with pytest.raises(AgentError, match="state_overlaps_source"):
        Config.load(path)


def test_private_credentials_remain_stable_and_token_is_not_repr(tmp_path):
    value = {
        "agent_id": str(uuid4()),
        "credential_revision": 1,
        "token": "secret" * 8,
        "expires_at": int(time.time()) + 120,
    }
    path = tmp_path / "credentials.json"
    private_write(path, value)
    credentials = Credentials.load(path)
    assert credentials == Credentials.load(path)
    assert value["token"] not in repr(credentials)
    value["database_url"] = "forbidden"
    private_write(path, value)
    with pytest.raises(AgentError, match="invalid_credentials"):
        Credentials.load(path)


def test_state_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AgentError, match="private_state_directory_required"):
        Journal(link)


def test_cli_check_contains_no_credentials_or_absolute_roots(
    agent, tmp_path, capsys, monkeypatch
):
    config, credentials, _, _, _ = agent
    value = config_value(tmp_path)
    value["credential_file"] = str(config.credential_file)
    value["state_dir"] = str(config.state_dir)
    path = tmp_path / "agent.json"
    private_write(path, value)
    private_write(
        config.credential_file,
        {
            "agent_id": credentials.agent_id,
            "credential_revision": 1,
            "token": credentials.token,
            "expires_at": credentials.expires_at,
        },
    )
    monkeypatch.setattr(
        "sys.argv", ["aap-storage-agent", "--config", str(path), "--check"]
    )
    from storage_agent.__main__ import main

    assert main() == 0
    output = capsys.readouterr().out
    assert credentials.token not in output and str(tmp_path) not in output
    assert json.loads(output)["root_handles"] == ["images"]
