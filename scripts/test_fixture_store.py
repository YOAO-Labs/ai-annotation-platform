import hashlib
import json
import shlex
import signal
import uuid

import pytest

from fixture_store import (
    _native_output_accounts,
    _native_output_iam,
    _native_output_paths,
    _native_output_policy,
)
from storage_s3_contract import ContractError
from verify_storage_candidates import Owner, private_write


_ATTACH_ARGS = ("test-policy", "--user", "test-user")
_ATTACH_COMMAND = "--json admin policy attach fixture " + " ".join(_ATTACH_ARGS)
_PRIVATE_MARKER = "private-test-value"


def _install_mc(tmp_path, commands):
    script = tmp_path / "mc"
    lines = ["#!/bin/sh", 'case "$*" in']
    for command, body in commands.items():
        lines.extend([f"  {shlex.quote(command)})", body, "    ;;"])
    lines.extend(["  *) exit 98 ;;", "esac", ""])
    script.write_text("\n".join(lines))
    script.chmod(0o700)
    return {"PATH": str(tmp_path)}


def _reply(payload, *, rc=0):
    text = shlex.quote(json.dumps(payload))
    return f"printf '%s\\n' {text}\nprintf '%s\\n' {_PRIVATE_MARKER} >&2\nexit {rc}"


def _fatal(code):
    return {
        "status": "error",
        "error": {
            "type": "fatal",
            "message": _PRIVATE_MARKER,
            "cause": {
                "message": _PRIVATE_MARKER,
                "error": {"Code": code, "Message": _PRIVATE_MARKER},
            },
        },
    }


@pytest.mark.parametrize(
    ("payload", "rc", "reason"),
    [
        pytest.param(_fatal("AccessDenied"), 1, "access_denied", id="named-denial"),
        pytest.param(
            _fatal("Unknown-" + _PRIVATE_MARKER), 1, "nonzero", id="unknown-code"
        ),
        pytest.param(_fatal(["AccessDenied"]), 1, "nonzero", id="malformed-code"),
        pytest.param(_fatal("AccessDenied"), 17, "nonzero", id="unexpected-exit"),
        pytest.param({"status": "success"}, 1, "nonzero", id="nonzero-success-json"),
    ],
)
def test_native_iam_attach_never_accepts_nonzero(tmp_path, payload, rc, reason):
    env = _install_mc(tmp_path, {_ATTACH_COMMAND: _reply(payload, rc=rc)})
    with pytest.raises(ContractError) as caught:
        _native_output_iam(env, "policy_attach", *_ATTACH_ARGS)
    assert str(caught.value) == f"fixture_native_iam_policy_attach_{reason}"


def test_native_iam_attach_redacts_opaque_output(tmp_path):
    env = _install_mc(
        tmp_path,
        {
            _ATTACH_COMMAND: (
                f"printf '%s\\n' {_PRIVATE_MARKER}\n"
                f"printf '%s\\n' {_PRIVATE_MARKER} >&2\n"
                "exit 1"
            )
        },
    )
    with pytest.raises(ContractError) as caught:
        _native_output_iam(env, "policy_attach", *_ATTACH_ARGS)
    assert str(caught.value) == "fixture_native_iam_policy_attach_nonzero"


def test_native_iam_attach_reports_process_signal(tmp_path):
    env = _install_mc(tmp_path, {_ATTACH_COMMAND: 'kill -TERM "$$"'})
    with pytest.raises(ContractError) as caught:
        _native_output_iam(env, "policy_attach", *_ATTACH_ARGS)
    assert str(caught.value) == (
        f"fixture_native_iam_policy_attach_signal_{signal.SIGTERM.value}"
    )


def test_native_iam_attach_accepts_clean_exit(tmp_path):
    env = _install_mc(tmp_path, {_ATTACH_COMMAND: _reply({"status": "success"})})
    assert _native_output_iam(env, "policy_attach", *_ATTACH_ARGS) is None


def test_native_iam_user_info_distinguishes_provider_absence(tmp_path):
    env = _install_mc(
        tmp_path,
        {
            "--json admin user info fixture test-user": _reply(
                _fatal("XMinioAdminNoSuchUser"), rc=1
            )
        },
    )
    with pytest.raises(ContractError) as caught:
        _native_output_iam(env, "user_info", "test-user")
    assert str(caught.value) == "fixture_native_iam_user_info_no_such_user"


def test_native_iam_policy_info_refuses_nonobject_json(tmp_path):
    env = _install_mc(
        tmp_path,
        {"--json admin policy info fixture test-policy": _reply(_PRIVATE_MARKER)},
    )
    with pytest.raises(ContractError) as caught:
        _native_output_iam(env, "policy_info", "test-policy")
    assert str(caught.value) == "fixture_native_iam_policy_info_response_invalid"


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param(None, id="exact-owned-policies"),
        pytest.param("disabled", id="disabled-user"),
        "extra-policy",
        "group-membership",
        "policy-document",
    ],
)
def test_native_iam_accounts_require_exact_enabled_roles(tmp_path, monkeypatch, drift):
    owner = Owner(tmp_path / "owner.json", create=True)
    paths = _native_output_paths(owner, uuid.uuid4())
    store = {"admin_endpoint": "http://localhost:9000"}
    private_write(
        owner.private / "minio.env",
        b"MINIO_ROOT_USER=test-root\nMINIO_ROOT_PASSWORD=test-root-secret\n",
    )
    for role, key, filename in (
        ("original", "credentials_file", "data-plane.json"),
        ("original-reader", "reader_file", "read-plane.json"),
    ):
        path = owner.private / filename
        private_write(path, json.dumps(_account(role)).encode())
        store[key] = str(path)
    roles = ("writer", "reader", "cleanup")
    accounts = {role: _account(role) for role in roles}
    private_write(paths["seed"], json.dumps(accounts).encode())
    entry = {"bucket": "test-bucket"}
    commands = {}
    for role in roles:
        access = accounts[role]["access_key"]
        name = entry[role + "_policy"] = f"test-policy-{role}"
        policy = _native_output_policy(entry["bucket"], role=role)
        private_write(paths[role + "_policy"], json.dumps(policy).encode())
        info: dict[str, object] = {
            "status": "success",
            "accessKey": access,
            "userStatus": "enabled",
            "policyName": name,
        }
        if role == "reader":
            if drift == "disabled":
                info["userStatus"] = "disabled"
            elif drift == "extra-policy":
                info["policyName"] = f"{name},unowned-policy"
            elif drift == "group-membership":
                info["memberOf"] = [{"name": "unowned-group"}]
            elif drift == "policy-document":
                policy["Statement"][0]["Resource"] = ["*"]
        commands[f"--json admin user info fixture {access}"] = _reply(info)
        commands[f"--json admin policy info fixture {name}"] = _reply(
            {"status": "success", "policyInfo": {"Policy": policy}}
        )
    monkeypatch.setenv("PATH", _install_mc(tmp_path, commands)["PATH"])
    if drift:
        with pytest.raises(
            ContractError, match="^fixture_native_account_policy_changed$"
        ):
            _native_output_accounts(owner, store, entry, paths)
    else:
        observed, evidence = _native_output_accounts(owner, store, entry, paths)
        assert observed == accounts
        assert set(evidence) == set(roles)
        for role in roles:
            assert evidence[role]["policy_name"] == entry[role + "_policy"]
            assert (
                evidence[role]["identity_sha256"]
                == hashlib.sha256(accounts[role]["access_key"].encode()).hexdigest()
            )


def _account(role):
    return {"access_key": f"test-{role}", "secret_key": f"test-{role}-secret"}
