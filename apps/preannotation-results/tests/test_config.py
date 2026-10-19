from __future__ import annotations

from pathlib import Path

import pytest

from aap_preannotation_results_runtime.config import (
    ConfigurationError,
    load_config,
)


def test_scoped_database_url_loads_inline_and_from_file(tmp_path: Path):
    secret = "postgresql://service:private@localhost/annotation"
    inline = load_config({"PREANNOTATION_RESULTS_DATABASE_URL": secret})
    assert inline.database_url == secret
    assert "private" not in repr(inline)

    secret_file = tmp_path / "database-url"
    secret_file.write_text(secret + "\n", encoding="utf-8")
    loaded = load_config({"PREANNOTATION_RESULTS_DATABASE_URL_FILE": str(secret_file)})
    assert loaded.database_url == secret


def test_scoped_database_url_rejects_ambiguous_or_non_postgres_values():
    with pytest.raises(ConfigurationError):
        load_config(
            {
                "PREANNOTATION_RESULTS_DATABASE_URL": "postgresql://localhost/db",
                "PREANNOTATION_RESULTS_DATABASE_URL_FILE": "/unused",
            }
        )
    with pytest.raises(ConfigurationError):
        load_config({"PREANNOTATION_RESULTS_DATABASE_URL": "https://example.test"})
