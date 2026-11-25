"""配置加载与保存, 供 SDK / CLI / TUI 共用。

读取顺序: 显式参数 > 环境变量 YOAO_BASE_URL / YOAO_API_KEY > ~/.config/yoao/config.toml。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from yoao.errors import SDKError

ENV_BASE_URL = "YOAO_BASE_URL"
ENV_API_KEY = "YOAO_API_KEY"


def config_path() -> Path:
    return Path.home() / ".config" / "yoao" / "config.toml"


def _read_file_config() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def load_config(
    base_url: str | None = None, api_key: str | None = None
) -> tuple[str | None, str | None]:
    """按优先级解析 (base_url, api_key); 任一项可能为 None。"""
    file_cfg = _read_file_config()
    base_url = base_url or os.environ.get(ENV_BASE_URL) or file_cfg.get("base_url")
    api_key = api_key or os.environ.get(ENV_API_KEY) or file_cfg.get("api_key")
    return base_url, api_key


def migrate_legacy_config() -> Path:
    """Copy the legacy configuration once without replacing an existing target."""
    source = Path.home() / ".config" / "ai-annotation" / "config.toml"
    target = config_path()
    try:
        content = source.read_bytes()
    except FileNotFoundError:
        raise SDKError("Legacy configuration does not exist.") from None
    except OSError:
        raise SDKError("Unable to read legacy configuration.") from None
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SDKError(
            "Target configuration already exists; refusing to overwrite it."
        ) from None
    except OSError:
        raise SDKError("Unable to create target configuration.") from None
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(content)
    except OSError:
        target.unlink(missing_ok=True)
        raise SDKError("Unable to write target configuration.") from None
    return target


def _toml_basic_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")


def save_config(base_url: str, api_key: str) -> Path:
    """以仅本人可读写的 TOML 文件原子替换登录配置。"""
    p = config_path()
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(f"base_url = {_toml_basic_string(base_url)}\n")
                stream.write(f"api_key = {_toml_basic_string(api_key)}\n")
            os.replace(temporary, p)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, UnicodeError):
        raise SDKError("Unable to save configuration.") from None
    return p
