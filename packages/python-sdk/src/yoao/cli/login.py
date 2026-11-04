"""yoao login: 验证凭据连通后写入 config.toml。"""

from __future__ import annotations

from typing import Optional

import typer

from yoao import Client
from yoao.cli._output import cli_errors, console, print_json
from yoao.config import load_config, migrate_legacy_config, save_config

DEFAULT_URL = "http://localhost:8000"


def login(
    url: Optional[str] = typer.Option(
        None, "--url", help="平台地址, 如 http://localhost:8000; 省略时交互式输入"
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="API key; 省略时交互式隐藏输入"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """验证凭据并写入 ~/.config/yoao/config.toml (权限 0600)。"""
    if url is None:
        # 默认值优先复用已存配置的 base_url, 否则回退到本地默认地址
        saved_url, _ = load_config()
        url = typer.prompt("平台地址", default=saved_url or DEFAULT_URL)
    if api_key is None:
        api_key = typer.prompt("API key", hide_input=True)
    with cli_errors(json_output):
        # 先用给定凭据调一个轻量 GET 验证连通; 失败则不落盘
        with Client(base_url=url, api_key=api_key) as client:
            client.projects.list()
        path = save_config(url, api_key)
    if json_output:
        print_json({"config_path": str(path), "base_url": url})
    else:
        console.print(f"[green]登录成功[/green], 配置已写入 {path}")
        console.print("文件权限已设为 0600 (含敏感 api_key, 请勿提交到版本库)")


def migrate_config(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """一次性复制旧版配置；保留原文件且不覆盖已有目标。"""
    with cli_errors(json_output):
        path = migrate_legacy_config()
    if json_output:
        print_json({"config_path": str(path)})
    else:
        console.print(f"配置已迁移至 {path}；原文件保留。")
