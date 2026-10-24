from __future__ import annotations

from pathlib import Path

import pytest

from aap_jobs.queue_config import JobsQueueSettings


def test_queue_settings_read_direct_or_file_secrets(tmp_path: Path):
    token_file = tmp_path / "token"
    broker_file = tmp_path / "broker"
    token_file.write_text("private-token\n", encoding="utf-8")
    broker_file.write_text("redis://broker.example/5\n", encoding="utf-8")

    settings = JobsQueueSettings.from_env(
        {
            "JOBS_QUEUE_BROKER_URL_FILE": str(broker_file),
            "JOBS_QUEUE_TOKEN_FILE": str(token_file),
            "JOBS_QUEUE_HOST": "0.0.0.0",
            "JOBS_QUEUE_PORT": "8190",
        }
    )
    assert settings.broker_url == "redis://broker.example/5"
    assert settings.token == "private-token"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8190
    assert "private-token" not in repr(settings)
    assert "broker.example" not in repr(settings)


def test_queue_settings_reject_conflicting_and_unbounded_secrets(tmp_path: Path):
    with pytest.raises(ValueError):
        JobsQueueSettings.from_env(
            {
                "JOBS_QUEUE_BROKER_URL": "redis://broker.example/5",
                "JOBS_QUEUE_TOKEN": "token",
                "JOBS_QUEUE_TOKEN_FILE": "/run/secrets/token",
            }
        )

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError):
        JobsQueueSettings.from_env(
            {
                "JOBS_QUEUE_BROKER_URL": "redis://broker.example/5",
                "JOBS_QUEUE_TOKEN_FILE": str(oversized),
            }
        )
