import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("WORKSPACE_MEMBERSHIP_URL", "env"),
        ("WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE", "env"),
        ("WORKSPACE_CONFIGURATION_DATABASE_URL", "dotenv"),
        ("WORKSPACE_TEMPLATE_QUERY_URL", "dotenv"),
        ("WORKSPACE_PROJECT_QUERY_DATABASE_URL", "env"),
        ("WORKSPACE_DIRECTORY_QUERY_DATABASE_URL_FILE", "dotenv"),
    ],
)
def test_split_workspace_binding_refuses_startup_without_disclosing_value(
    monkeypatch, tmp_path, key, source
):
    env_file = tmp_path / ".env"
    value = "private-deployment-value"
    if source == "env":
        monkeypatch.setenv(key, value)
    else:
        monkeypatch.delenv(key, raising=False)
        env_file.write_text(f"{key}={value}\n")

    with pytest.raises(RuntimeError, match=key) as error:
        Settings(_env_file=env_file)
    assert value not in str(error.value)


def test_unselected_workspace_does_not_disable_independent_probe(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WORKSPACE_CONFIGURATION_URL=http://old-workspace.invalid\n")
    monkeypatch.setenv("WORKSPACE_CONFIGURATION_URL", "")
    monkeypatch.setenv("INFERENCE_BACKEND_PROBE_URL", "http://inference.invalid")

    assert Settings(_env_file=env_file).inference_backend_probe_url == (
        "http://inference.invalid"
    )
