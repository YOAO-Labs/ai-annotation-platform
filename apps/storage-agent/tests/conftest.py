import os
from pathlib import Path
import time
from uuid import uuid4

import pytest

from aap_filesystem_read import RootSpec, capture_anchor

from storage_agent.config import Config, Credentials
from storage_agent.journal import Journal
from storage_agent.runner import Runner


@pytest.fixture
def agent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "image.bin").write_bytes(b"source pixels")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = Config(
        "https://center.example",
        state,
        tmp_path / "credentials.json",
        {"images": RootSpec("images", str(source))},
    )
    credentials = Credentials(str(uuid4()), 1, "s" * 40, int(time.time()) + 3600)
    journal = Journal(state)
    with journal:
        yield config, credentials, journal, Runner(config, credentials, journal), source


@pytest.fixture
def command(agent):
    config, credentials, _, _, _ = agent
    now = int(time.time())
    return {
        "protocol_version": 1,
        "command_id": str(uuid4()),
        "agent_id": credentials.agent_id,
        "credential_revision": 1,
        "root_handle": "images",
        "root_revision": 1,
        "operation": "read",
        "issued_at": now,
        "expires_at": now + 60,
        "max_bytes": 1024 * 1024,
        "timeout_seconds": 10,
        "arguments": {
            "relative_path": "",
            "expected_anchor": capture_anchor(config.roots["images"]),
            "path": "image.bin",
        },
        "fence": {
            "location_id": str(uuid4()),
            "location_revision": 1,
            "maintenance_revision": 1,
            "purpose": "reference_read",
        },
    }


def private_write(path: Path, value):
    import json

    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
