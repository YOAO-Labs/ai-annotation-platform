"""ML Backend 实时统计 WS 消费器（仅 TUI 用）。

订阅后端 `/api/v1/ws/ml-backend-stats`（每 1s 推 `{"backends": [...], "timestamp": ...}`），
反序列化为 `MLBackendStatsSnapshot` 列表后回调给 TUI。跑在 Textual 的 asyncio loop 里，
**不**碰同步 `Client` / httpx；连接失败 / 断线时指数退避重连，由调用方 cancel 停止。

后端 WS 鉴权接受 `ak_` api_key（见 ws.py），故这里直接用 SDK 的 api_key。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import quote

from ai_annotation.models import MLBackendStatsSnapshot

log = logging.getLogger(__name__)

# 后端 super_admin / project_admin 才放行；其余 close。这两个 code 不重连（鉴权问题重连也没用）。
_AUTH_CLOSE_CODES = {1008}

SnapshotHandler = Callable[[list[MLBackendStatsSnapshot]], Awaitable[None] | None]


def build_ws_url(base_url: str, api_key: str) -> str:
    """把 http(s) base_url 拼成 ml-backend-stats 的 ws(s) URL（token 走 query）。

    注意：ws_router 在后端 main.py 无 prefix 注册，路径是 `/ws/...` 而非 `/api/v1/ws/...`。
    """
    root = base_url.rstrip("/")
    if root.startswith("https://"):
        root = "wss://" + root[len("https://") :]
    elif root.startswith("http://"):
        root = "ws://" + root[len("http://") :]
    return f"{root}/ws/ml-backend-stats?token={quote(api_key, safe='')}"


def parse_frame(text: str) -> list[MLBackendStatsSnapshot] | None:
    """解析一条 WS 文本帧。心跳 / 非统计帧返回 None；统计帧返回快照列表。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "backends" not in data:
        return None  # {"type": "ping"} 心跳或其他帧
    out: list[MLBackendStatsSnapshot] = []
    for raw in data.get("backends") or []:
        try:
            out.append(MLBackendStatsSnapshot.model_validate(raw))
        except Exception as exc:  # 单条坏数据不拖垮整帧
            log.debug("ml-stats snapshot parse skip: %s", exc)
    return out


class MlStatsStream:
    """长驻 WS 订阅。`run()` 循环连接 + 重连直到被 cancel；每帧调用 on_snapshots。

    `on_error(msg)` 可选：用于把鉴权失败 / 连接异常透出给 UI 做降级提示。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        on_snapshots: SnapshotHandler,
        on_error: Callable[[str], None] | None = None,
        *,
        max_backoff: float = 30.0,
    ):
        self._url = build_ws_url(base_url, api_key)
        self._on_snapshots = on_snapshots
        self._on_error = on_error
        self._max_backoff = max_backoff

    async def _emit(self, snaps: list[MLBackendStatsSnapshot]) -> None:
        res = self._on_snapshots(snaps)
        if asyncio.iscoroutine(res):
            await res

    async def run(self) -> None:
        # 延迟导入：websockets 仅在 [tui] extra 中，避免核心包硬依赖。
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._url, open_timeout=10) as ws:
                    backoff = 1.0  # 连上即重置退避
                    async for message in ws:
                        text = message if isinstance(message, str) else message.decode()
                        snaps = parse_frame(text)
                        if snaps is not None:
                            await self._emit(snaps)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 连接层任何异常都重试
                code = getattr(exc, "code", None)
                if code in _AUTH_CLOSE_CODES:
                    if self._on_error:
                        self._on_error("实时统计鉴权失败（需 admin 权限的 key）")
                    return  # 鉴权问题不重连
                if self._on_error:
                    self._on_error(f"实时统计连接中断，{backoff:g}s 后重连")
                log.debug("ml-stats ws error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
