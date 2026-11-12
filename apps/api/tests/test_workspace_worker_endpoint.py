"""Private worker routing must not rewrite frozen non-loopback endpoints."""

import pytest

from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:9000", "host.docker.internal:9000"),
        ("https://127.0.0.2:9443", "host.docker.internal:9443"),
        ("http://[::1]:9000", "host.docker.internal:9000"),
        ("https://localhost", "host.docker.internal"),
        ("http://172.19.0.1:9000", "172.19.0.1:9000"),
        ("https://Store.Internal:09443", "Store.Internal:09443"),
        ("http://[fd00::5]:9000", "[fd00::5]:9000"),
    ],
)
def test_worker_preserves_non_loopback_storage_authority(endpoint, expected):
    assert WorkspaceCeleryWorker._docker_http_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://@store.internal:9000",
        "http://user:secret@store.internal:9000",
        "http://store.internal:9000/path",
        "http://store.internal:9000?query=1",
        "http://store.internal:9000#fragment",
    ],
)
def test_worker_rejects_non_endpoint_authorities(endpoint):
    with pytest.raises(ValueError):
        WorkspaceCeleryWorker._docker_http_endpoint(endpoint)
