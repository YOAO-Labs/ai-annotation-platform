"""Shared protocol peer and private file helpers for the storage agent tests."""

import json
import os
from pathlib import Path

from yoao_site_agent_contract import canonical_json, digest


def acknowledgement(result, state="applied"):
    return {
        "protocol_version": 1,
        "command_id": result["command_id"],
        "command_digest": result["command_digest"],
        "result_digest": digest(result),
        "state": state,
        "code": "lease_expired" if state == "rejected" else None,
    }


def config_value(tmp_path):
    return {
        "center_url": "https://center.example",
        "state_dir": str(tmp_path / "state"),
        "credential_file": str(tmp_path / "credentials.json"),
        "roots": {"images": {"path": str(tmp_path / "source"), "revision": 1}},
    }


def private_write(path: Path, value):
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)


class Response:
    status = 200

    def __init__(self, value):
        self.data = canonical_json(value)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.data[:limit]


class Opener:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def open(self, req, timeout):
        self.calls.append(req)
        return self.callback(req)
