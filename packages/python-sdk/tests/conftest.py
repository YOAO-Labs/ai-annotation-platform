"""共享 fixtures: 指向 mock server 的 Client (respx 拦截所有 HTTP)。"""

import pytest

from yoao import Client

BASE = "http://testserver"
API = f"{BASE}/api/v1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("YOAO_BASE_URL", raising=False)
    monkeypatch.delenv("YOAO_API_KEY", raising=False)
    c = Client(base_url=BASE, api_key="ak_test")
    yield c
    c.close()
