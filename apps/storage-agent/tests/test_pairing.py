from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from uuid import UUID, uuid4

import pytest

from storage_agent.config import AgentError, Config, Credentials
from storage_agent import pairing
from storage_agent.journal import Journal

from conftest import private_write
from test_agent import config_value
from test_client import Opener, Response, tls_center as tls_center


@pytest.fixture
def pairing_case(tmp_path):
    config_path = tmp_path / "agent.json"
    private_write(config_path, config_value(tmp_path))
    config = Config.load(config_path)
    request_path = tmp_path / "pairing.json"
    value = {"agent_id": str(uuid4()), "pair_code": "pair_code_" * 5}
    private_write(request_path, value)
    response = {
        "agent_id": value["agent_id"],
        "credential_revision": 1,
        "token": "independent_identity_" * 3,
        "expires_at": int(time.time()) + 3600,
    }
    return config, request_path, value, response


def test_actual_tls_pairing_persists_private_identity_without_auth(
    pairing_case, tls_center
):
    config, request_path, value, response = pairing_case
    origin, ca, peer = tls_center
    peer["pair_response"] = response
    config = replace(config, center_url=origin, ca_file=ca)
    credentials = pairing.pair(config, request_path)
    assert asdict(credentials) == response
    assert Credentials.load(config.credential_file) == credentials
    assert stat.S_IMODE(config.credential_file.stat().st_mode) == 0o600
    assert peer["pair_requests"] == [{**value, "protocol_version": 1}]
    assert peer["paths"] == [pairing.PAIRING_PATH]
    assert peer["authorization"] == [None] and peer["sessions"] == [None]
    assert not list(config.credential_file.parent.glob(".agent-identity-*"))


def test_actual_tls_pairing_has_no_proxy_redirect_or_untrusted_certificate(
    pairing_case, tls_center, monkeypatch
):
    config, request_path, _, response = pairing_case
    origin, ca, peer = tls_center
    peer["pair_response"] = response
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    with pytest.raises(AgentError, match="pairing_response_uncertain"):
        pairing.pair(replace(config, center_url=origin), request_path)
    assert peer["paths"] == [] and not config.credential_file.exists()
    peer["redirect"] = True
    with pytest.raises(AgentError, match="pairing_request_rejected"):
        pairing.pair(replace(config, center_url=origin, ca_file=ca), request_path)
    assert peer["paths"] == [pairing.PAIRING_PATH]
    assert not config.credential_file.exists()


@pytest.mark.parametrize("failure", ["disconnect", "server_error"])
def test_uncertain_one_time_pairing_is_never_automatically_retried(
    pairing_case, tls_center, failure
):
    config, request_path, _, response = pairing_case
    origin, ca, peer = tls_center
    peer["pair_response"] = response
    peer["drop_pair_response"] = failure == "disconnect"
    peer["pair_status"] = 503 if failure == "server_error" else 200
    with pytest.raises(AgentError):
        pairing.pair(replace(config, center_url=origin, ca_file=ca), request_path)
    assert len(peer["pair_requests"]) == 1
    assert not config.credential_file.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"agent_id": str(uuid4())},
        {"token": "short"},
        {"token": "unsafe?" * 8},
        {"credential_revision": True},
        {"credential_revision": 0},
        {"expires_at": True},
        {"expires_at": 1},
        {"expires_at": int(time.time()) + pairing.MAX_CREDENTIAL_SECONDS + 120},
        {"database_url": "forbidden"},
    ],
)
def test_pairing_response_must_be_exact_matching_and_bounded(pairing_case, mutation):
    config, request_path, _, response = pairing_case
    opener = Opener(lambda _: Response({**response, **mutation}))
    with pytest.raises(AgentError):
        pairing.pair(config, request_path, opener=opener)
    assert len(opener.calls) == 1 and not config.credential_file.exists()


def test_oversized_response_is_rejected_after_one_bounded_read(pairing_case):
    config, request_path, _, response = pairing_case
    sizes = []

    class Oversized(Response):
        def read(self, limit):
            sizes.append(limit)
            return b" " * limit

    opener = Opener(lambda _: Oversized(response))
    with pytest.raises(AgentError, match="invalid_pairing_response"):
        pairing.pair(config, request_path, opener=opener)
    assert sizes == [pairing.MAX_RESPONSE_BYTES + 1]
    assert len(opener.calls) == 1 and not config.credential_file.exists()


@pytest.mark.parametrize("mode", [0o644, 0o400])
def test_pairing_file_must_be_owned_regular_and_exactly_0600(pairing_case, mode):
    config, request_path, _, _ = pairing_case
    request_path.chmod(mode)
    opener = Opener(lambda _: pytest.fail("Unsafe pairing file must not be sent"))
    with pytest.raises(AgentError, match="private_pairing_file_required"):
        pairing.pair(config, request_path, opener=opener)
    assert not opener.calls


def test_pairing_file_symlink_is_rejected_before_request(pairing_case):
    config, request_path, _, _ = pairing_case
    link = request_path.with_name("pairing-link.json")
    link.symlink_to(request_path)
    opener = Opener(lambda _: pytest.fail("Symlink pairing file must not be sent"))
    with pytest.raises(AgentError, match="private_pairing_file_required"):
        pairing.pair(config, link, opener=opener)


@pytest.mark.parametrize(
    "mutation",
    [
        {"agent_id": "invalid"},
        {"pair_code": "short"},
        {"pair_code": "x" * 257},
        {"pair_code": "?" * 40},
        {"protocol_version": 1},
        {"token": "forbidden" * 8},
    ],
)
def test_pairing_file_only_accepts_two_exact_fields(pairing_case, mutation):
    config, request_path, value, _ = pairing_case
    private_write(request_path, {**value, **mutation})
    opener = Opener(lambda _: pytest.fail("Invalid pairing file must not be sent"))
    with pytest.raises(AgentError, match="invalid_pairing_request"):
        pairing.pair(config, request_path, opener=opener)


@pytest.mark.parametrize("existing", ["identity", "broken_symlink"])
def test_existing_identity_fails_before_consuming_pair_code(pairing_case, existing):
    config, request_path, _, response = pairing_case
    if existing == "identity":
        private_write(config.credential_file, response)
    else:
        config.credential_file.symlink_to("missing-identity")
    opener = Opener(lambda _: pytest.fail("Existing identity must never be replaced"))
    with pytest.raises(AgentError, match="credential_file_exists_use_new_path"):
        pairing.pair(config, request_path, opener=opener)
    assert not opener.calls
    if existing == "identity":
        assert json.loads(config.credential_file.read_text()) == response
    else:
        assert config.credential_file.is_symlink()


def test_identity_created_during_request_is_preserved(pairing_case):
    config, request_path, _, response = pairing_case
    existing = {**response, "token": "existing_identity_" * 3}

    def concurrent_identity(_):
        private_write(config.credential_file, existing)
        return Response(response)

    with pytest.raises(AgentError, match="credential_file_exists_use_new_path"):
        pairing.pair(config, request_path, opener=Opener(concurrent_identity))
    assert json.loads(config.credential_file.read_text()) == existing
    assert not list(config.credential_file.parent.glob(".agent-identity-*"))


def test_new_credential_parent_directories_are_private(pairing_case):
    config, request_path, _, response = pairing_case
    parent = config.credential_file.parent / "new-parent" / "identity"
    config = replace(config, credential_file=parent / "agent.json")
    pairing.pair(config, request_path, opener=Opener(lambda _: Response(response)))
    assert Credentials.load(config.credential_file).agent_id == response["agent_id"]
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.parent.stat().st_mode) == 0o700


def test_shared_credential_parent_is_rejected_before_request(pairing_case):
    config, request_path, _, response = pairing_case
    parent = config.credential_file.parent / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    config = replace(config, credential_file=parent / "agent.json")
    opener = Opener(lambda _: Response(response))
    with pytest.raises(AgentError, match="private_credential_parent_required"):
        pairing.pair(config, request_path, opener=opener)
    assert not opener.calls


@pytest.mark.parametrize("failure", ["partial_write", "file_fsync", "replace"])
def test_failed_persistence_never_publishes_partial_identity(
    pairing_case, monkeypatch, failure
):
    config, request_path, _, response = pairing_case
    if failure == "partial_write":
        original = pairing.os.fdopen

        class PartialWriter:
            def __init__(self, *args, **kwargs):
                self.body = original(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.body.close()

            def write(self, data):
                return self.body.write(data[:5])

        monkeypatch.setattr(
            pairing.os,
            "fdopen",
            lambda fd, mode, **kwargs: (
                PartialWriter(fd, mode, **kwargs)
                if mode == "wb"
                else original(fd, mode, **kwargs)
            ),
        )
    elif failure == "file_fsync":
        monkeypatch.setattr(
            pairing.os, "fsync", lambda _: (_ for _ in ()).throw(OSError())
        )
    else:
        monkeypatch.setattr(
            pairing,
            "_publish",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
        )
    with pytest.raises(AgentError, match="credential_persist_failed"):
        pairing.pair(config, request_path, opener=Opener(lambda _: Response(response)))
    assert not config.credential_file.exists()
    assert not list(config.credential_file.parent.glob(".agent-identity-*"))


def test_credential_write_flushes_before_replace_and_parent_sync(
    pairing_case, monkeypatch
):
    config, request_path, _, response = pairing_case
    original_sync, original_replace = pairing.os.fsync, pairing._publish
    calls = []

    def sync(fd):
        calls.append(
            "directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync"
        )
        return original_sync(fd)

    def replace_file(*args, **kwargs):
        calls.append("replace")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(pairing.os, "fsync", sync)
    monkeypatch.setattr(pairing, "_publish", replace_file)
    pairing.pair(config, request_path, opener=Opener(lambda _: Response(response)))
    assert calls == ["file_sync", "replace", "directory_sync"]


def test_atomic_publish_never_overwrites_identity_created_at_final_rename(
    pairing_case, monkeypatch
):
    config, request_path, _, response = pairing_case
    existing = {**response, "token": "race_existing_identity_" * 3}
    original_publish = pairing._publish

    def concurrent_publish(parent_fd, temporary, destination):
        private_write(config.credential_file, existing)
        original_publish(parent_fd, temporary, destination)

    monkeypatch.setattr(pairing, "_publish", concurrent_publish)
    with pytest.raises(AgentError, match="credential_file_exists_use_new_path"):
        pairing.pair(config, request_path, opener=Opener(lambda _: Response(response)))
    assert json.loads(config.credential_file.read_text()) == existing
    assert not list(config.credential_file.parent.glob(".agent-identity-*"))


def test_parent_sync_failure_keeps_complete_published_identity(
    pairing_case, monkeypatch
):
    config, request_path, _, response = pairing_case
    original_sync = pairing.os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("Directory durability unconfirmed")
        original_sync(fd)

    monkeypatch.setattr(pairing.os, "fsync", sync)
    with pytest.raises(AgentError, match="credential_persist_failed"):
        pairing.pair(config, request_path, opener=Opener(lambda _: Response(response)))
    assert asdict(Credentials.load(config.credential_file)) == response
    assert not list(config.credential_file.parent.glob(".agent-identity-*"))


def test_pairing_cli_outputs_no_secret_and_check_needs_no_identity(
    pairing_case, capsys, monkeypatch
):
    config, request_path, value, response = pairing_case
    config_path = request_path.with_name("agent.json")
    from storage_agent.__main__ import main

    monkeypatch.setattr(
        "sys.argv", ["aap-storage-agent", "--config", str(config_path), "--check"]
    )
    assert main() == 0 and not config.credential_file.exists()
    output = capsys.readouterr()
    assert json.loads(output.out)["root_handles"] == ["images"]
    opener = Opener(lambda _: Response(response))
    monkeypatch.setattr(pairing, "center_opener", lambda _: opener)
    monkeypatch.setattr(
        "sys.argv",
        [
            "aap-storage-agent",
            "--config",
            str(config_path),
            "--pairing-file",
            str(request_path),
        ],
    )
    assert main() == 0
    output = capsys.readouterr()
    assert output.out == "agent_paired\n" and output.err == ""
    assert value["pair_code"] not in output.out and response["token"] not in output.out


@pytest.mark.parametrize("mode", ["--once", "--drain", "--check"])
def test_pairing_cli_modes_are_mutually_exclusive(pairing_case, monkeypatch, mode):
    _, request_path, _, _ = pairing_case
    from storage_agent.__main__ import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "aap-storage-agent",
            "--config",
            "unused",
            "--pairing-file",
            str(request_path),
            mode,
        ],
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_independent_process_restart_replays_received_result_with_new_session(
    agent, command, tls_center, tmp_path
):
    config, credentials, journal, _, source = agent
    origin, ca, peer = tls_center
    peer.update(drop_first_result=False, poll_command=command, ack_state="received")
    config_path = tmp_path / "agent.json"
    value = config_value(tmp_path)
    value.update(center_url=origin, ca_file=ca, state_dir=str(config.state_dir))
    private_write(config_path, value)
    private_write(config.credential_file, asdict(credentials))
    journal.close()
    repo = Path(__file__).resolve().parents[3]
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            str(repo / path)
            for path in (
                "apps/storage-agent",
                "apps/_shared/filesystem_read/src",
                "apps/_shared/site_agent_contract/src",
            )
        ),
    }

    def run(mode):
        result = subprocess.run(
            [sys.executable, "-m", "storage_agent", "--config", str(config_path), mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert credentials.token not in result.stdout + result.stderr

    run("--once")
    with Journal(config.state_dir) as current:
        assert current.get(command["command_id"])["state"] == "received"
        assert len(list(current.stages.iterdir())) == 1
    (source / "image.bin").write_bytes(b"new source after original result")
    first_session = peer["sessions"][0]
    peer["ack_state"] = "applied"
    run("--drain")
    with Journal(config.state_dir) as current:
        assert current.get(command["command_id"])["state"] == "applied"
        assert not list(current.stages.iterdir())
    assert len(peer["result_posts"]) == 3
    assert all(result == peer["result_posts"][0] for result in peer["result_posts"])
    assert list(peer["chunks"].values()) == [b"source pixels"]
    assert peer["chunk_posts"] == [b"source pixels"]
    assert peer["paths"].count("/api/v1/site-agents/commands/poll") == 1
    last_session = peer["sessions"][-1]
    assert UUID(first_session) and UUID(last_session) and first_session != last_session


def test_pairing_refuses_pending_old_identity_before_consuming_code(
    agent, command, tmp_path
):
    config, _, journal, runner, _ = agent
    result = runner.execute(command)
    journal.close()
    config = replace(config, credential_file=tmp_path / "new-identity.json")
    pairing_path = tmp_path / "pairing.json"
    private_write(
        pairing_path, {"agent_id": str(uuid4()), "pair_code": "new_pair_" * 5}
    )
    opener = Opener(lambda _: pytest.fail("Pending old work must be drained first"))
    with pytest.raises(
        AgentError, match="pending_commands_drain_old_config_before_pairing"
    ):
        pairing.pair(config, pairing_path, opener=opener)
    with Journal(config.state_dir) as current:
        assert current.get(command["command_id"])["result"] == result
        assert len(list(current.stages.iterdir())) == 1
    assert not opener.calls and not config.credential_file.exists()
