"""Deployment-pinned first-party backend identities and private Bearer auth."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


BACKEND_SERVICES = {
    "gsam2": "grounded-sam2-backend",
    "sam3": "sam3-backend",
    "yolo": "yolo-backend",
    "onnxtools": "onnxtools-backend",
    "rapidocr": "rapidocr-backend",
}


def normalized_backend_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) <= 32 or c == "\\" for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return ""
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return urlunsplit((parsed.scheme, f"{host}:{port}", "", "", ""))
    except ValueError:
        return ""


def validate_backend_urls(value: dict) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - BACKEND_SERVICES.keys():
        raise ValueError("Expected fixed first-party backend IDs")
    normalized = [normalized_backend_url(url) for url in value.values()]
    if not all(normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("Backend URLs must be valid, unique HTTP origins")
    return {key: url.rstrip("/") for key, url in value.items()}


def managed_provider(method: str) -> str | None:
    if isinstance(method, str) and method.startswith("managed:"):
        provider = method.removeprefix("managed:")
        return provider if provider in BACKEND_SERVICES else None
    return None
