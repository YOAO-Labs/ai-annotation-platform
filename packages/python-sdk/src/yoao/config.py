"""配置加载与保存, 供 SDK / CLI / TUI 共用。

读取顺序: 显式参数 > 环境变量 YOAO_BASE_URL / YOAO_API_KEY > ~/.config/yoao/config.toml。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

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


def save_config(base_url: str, api_key: str) -> Path:
    """写 config.toml (chmod 0600, api_key 是敏感凭据)。"""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 手写两行 toml, 避免引入写依赖; 值为 URL / token, 无需转义。
    # O_CREAT 0600 原子创建, 避免「先写后 chmod」窗口期 api_key 可被他人读取
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f'base_url = "{base_url}"\napi_key = "{api_key}"\n')
    p.chmod(0o600)  # 覆盖已存在文件时同样收紧权限
    return p
