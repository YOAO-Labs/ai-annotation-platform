import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from yoao_notifications.delivery_config import optional_notification_secret
from yoao_notifications.websocket import serve_notification_socket

from app.config import settings
from app.db.enums import PlatformRole
from app.services.identity_contracts import (
    IdentityActorStatus,
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_gateway import get_identity_gateway
from app.services.ml_stats_presence import ML_STATS_CHANNEL, MLStatsPresence

if TYPE_CHECKING:
    from yoao_identity.contract import Principal

from app.services.labeling_core_read_relations import (
    get_core_row,
    task_row,
    video_tracker_job_row,
)

router = APIRouter()
log = logging.getLogger(__name__)

_ADMIN_SOCKET_ROLES = (
    PlatformRole.SUPER_ADMIN.value,
    PlatformRole.PROJECT_ADMIN.value,
)


async def _load_active_user(user_id: str | None) -> IdentityActorStatus | None:
    """Resolve the current database account for a socket handshake.

    Socket tokens are long-lived: a JWT ``role`` claim can outlive a role
    change or account disable.  Restricted subscriptions must therefore
    re-resolve the account from the database instead of trusting the claim.
    Returns ``None`` when the account is missing or inactive.
    """

    if not user_id:
        return None
    from app.db.base import async_session

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return None
    async with async_session() as db:
        user = await IdentityAccountLockData(db).read_actor_status(user_uuid)
        if user is None or not user.is_active:
            return None
        return user


async def _authenticate_socket_token(
    token: str, *, allow_api_key: bool = True
) -> "Principal | None":
    """Resolve socket credentials through the shared Identity admission gateway."""

    if not allow_api_key and token.startswith("ak_"):
        return None
    try:
        return await get_identity_gateway().authenticate(token)
    except (IdentityCredentialRejected, IdentityGatewayUnavailable):
        return None


async def _revalidate_project_stream(token: str, project_id: uuid.UUID) -> bool:
    actor = await _authenticate_socket_token(token)
    if actor is None:
        return False
    return await _revalidate_project_access(project_id, str(actor.actor_id))


async def _notification_authenticate(token: str):
    return await _authenticate_socket_token(token, allow_api_key=False)


@asynccontextmanager
async def _open_notification_delivery(_principal):
    from app.db.base import async_session
    from app.services.notification import NotificationService

    async with async_session() as db:
        yield NotificationService(db)


async def _revalidate_task_stream(token: str, task_id: uuid.UUID) -> bool:
    actor = await _authenticate_socket_token(token, allow_api_key=False)
    if actor is None:
        return False
    return await _revalidate_task_visible(task_id, str(actor.actor_id))


async def _revalidate_admin_stream(token: str, *, allow_api_key: bool = False) -> bool:
    actor = await _authenticate_socket_token(token, allow_api_key=allow_api_key)
    return actor is not None and actor.role in _ADMIN_SOCKET_ROLES


async def _revalidate_project_access(project_id: uuid.UUID, user_id: str) -> bool:
    """Re-resolve current project access for an ongoing restricted stream.

    Project removal must stop future messages for that project while unrelated
    authorized sockets keep working.
    """

    from app.db.base import async_session
    from app.services.project_access import (
        get_workspace_project,
        resolve_project_access,
    )

    async with async_session() as db:
        project = await get_workspace_project(db, actor_id=None, project_id=project_id)
        user = await IdentityAccountLockData(db).read_actor_status(
            uuid.UUID(str(user_id))
        )
        if project is None or user is None:
            return False
        try:
            await resolve_project_access(db, actor_id=user.id, project=project)
        except Exception:
            return False
        return True


async def _revalidate_task_visible(task_id: uuid.UUID, user_id: str) -> bool:
    from app.services import task_access
    from app.db.base import async_session

    user = await _load_active_user(user_id)
    if user is None:
        return False
    async with async_session() as db:
        task = await get_core_row(db, task_row, task_id)
        if task is None:
            return False
        try:
            await task_access.assert_task_visible(db, task, user.id)
        except Exception:
            return False
        return True


_REDIS_POOL: ConnectionPool | None = None
_NOTIFICATION_REDIS_POOL: ConnectionPool | None = None


def _get_redis_pool() -> ConnectionPool:
    global _REDIS_POOL
    if _REDIS_POOL is None:
        _REDIS_POOL = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=200,
            decode_responses=False,
        )
    return _REDIS_POOL


def _get_notification_redis_pool() -> ConnectionPool:
    global _NOTIFICATION_REDIS_POOL
    if _NOTIFICATION_REDIS_POOL is None:
        url = optional_notification_secret(
            settings.notification_subscribe_redis_url,
            settings.notification_subscribe_redis_url_file,
        )
        if url is None:
            if settings.notification_publication_url:
                raise ValueError(
                    "Isolated Notifications requires a subscribe Redis source"
                )
            return _get_redis_pool()
        _NOTIFICATION_REDIS_POOL = ConnectionPool.from_url(
            url, max_connections=200, decode_responses=False
        )
    return _NOTIFICATION_REDIS_POOL


async def close_redis_pool() -> None:
    """uvicorn --reload / SIGTERM 时释放 Redis 连接池, 让悬挂 WS 收到 1006.

    现行架构无内存级 connection 表 (全靠 Redis pub/sub), 各 endpoint finally 块虽然
    会 close pubsub, 但 SIGTERM 路径下 finally 不一定执行 — disconnect 释放 socket,
    客户端 WS 收到 abnormal closure 后走自带的指数退避重连.

    重要: 必须 timeout 兜底. inuse_connections=True 会等持有 in-use 连接的 task 主动
    释放, 但 lifespan shutdown 阶段那些 task 未必已被 cancel (asyncio task 取消是
    协作式的); 不带 timeout 会让 worker 永久卡 "Waiting for background tasks to
    complete". 实测 2s 足够 pubsub.listen() 协程注意到 cancellation 退出.
    """
    global _REDIS_POOL, _NOTIFICATION_REDIS_POOL
    pools = (_REDIS_POOL, _NOTIFICATION_REDIS_POOL)
    _REDIS_POOL = _NOTIFICATION_REDIS_POOL = None
    for pool in pools:
        if pool is None:
            continue
        try:
            await asyncio.wait_for(pool.disconnect(inuse_connections=True), timeout=2)
        except Exception:
            log.debug("WebSocket Redis pool close failed; continuing shutdown")


HEARTBEAT_INTERVAL = 30


async def _heartbeat_loop(websocket: WebSocket) -> None:
    """每 30s 推一帧 ping。客户端不需响应（仅保活）。"""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception as e:
        log.debug("heartbeat loop ended: %s", e)


async def _run_pubsub_ws(
    websocket: WebSocket,
    pubsub: aioredis.client.PubSub,
    *,
    heartbeat: bool = True,
    revalidate=None,
    allow_message=None,
    maintain=None,
) -> None:
    """把 Redis pub/sub 消息转发给 WebSocket, 直到任意一方关闭。caller 负责 subscribe/cleanup。

    listen 转发循环与 websocket.receive() 并发跑: 单独阻塞在 pubsub.listen() 时
    感知不到断连, 优雅关闭 (uvicorn --reload / SIGTERM) 时 handler 会一直挂到
    timeout-graceful-shutdown 超时被强制取消 — 每次 reload 打一条 ERROR。uvicorn
    在 shutdown 时会向 receive 队列推 websocket.disconnect (code 1012, 客户端关闭同理),
    所以 await receive() 能让 handler 即时退出, 既消除 reload 卡顿, 也修复阻塞在
    listen() 时漏判客户端断开的问题。
    """

    async def _relay() -> None:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if revalidate is not None and not await revalidate():
                return
            data = message["data"]
            if allow_message is not None and not await allow_message(data):
                continue
            await websocket.send_text(
                data.decode() if isinstance(data, bytes) else data
            )

    async def _watch_disconnect() -> None:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return

    tasks = [
        asyncio.create_task(_relay()),
        asyncio.create_task(_watch_disconnect()),
    ]
    if heartbeat:
        tasks.append(asyncio.create_task(_heartbeat_loop(websocket)))
    if maintain is not None:
        tasks.append(asyncio.create_task(maintain()))

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(
            exc, (WebSocketDisconnect, asyncio.CancelledError)
        ):
            raise exc


@router.websocket("/ws/projects/{project_id}/preannotate")
async def preannotate_progress(
    websocket: WebSocket,
    project_id: uuid.UUID,
    token: str = Query(...),
):
    """Project preannotation progress.

    Handshake contract (B3 -> B4): connect with ``?token=<JWT or ak_ api_key>``.
    The stream re-checks current project access every
    :data:`REVALIDATE_INTERVAL` seconds so losing the project stops delivery.
    """

    actor = await _authenticate_socket_token(token)
    if actor is None or not await _revalidate_project_access(
        project_id, str(actor.actor_id)
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    channel = f"project:{project_id}:preannotate"
    await pubsub.subscribe(channel)
    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            heartbeat=False,
            revalidate=lambda: _revalidate_project_stream(token, project_id),
        )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass


@router.websocket("/ws/batches/project/{project_id}")
async def batch_events_socket(
    websocket: WebSocket,
    project_id: uuid.UUID,
    token: str = Query(...),
):
    """项目级 batch 状态变更广播.

    Channel: `project:{project_id}:batch` (BatchService.transition / check_auto_transitions
    在 publish_batch_status_change() 推 batch.status_changed 事件). 前端
    useBatchEventsSocket 收到后 invalidate ["batches", projectId], 让标注员/admin
    多端实时看到 batch 状态翻转 (B-15).

    Handshake contract (B3 -> B4): connect with ``?token=<JWT or ak_ api_key>``.
    Project access is required at handshake and re-checked while streaming so a
    removed member stops receiving this project's events.
    """

    actor = await _authenticate_socket_token(token)
    if actor is None or not await _revalidate_project_access(
        project_id, str(actor.actor_id)
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    channel = f"project:{project_id}:batch"
    await pubsub.subscribe(channel)
    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            revalidate=lambda: _revalidate_project_stream(token, project_id),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("batch-events WS error project=%s err=%s", project_id, e)
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass


@router.websocket("/ws/prediction-jobs")
async def prediction_jobs_socket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """全局 prediction job 进度通道 (Topbar 徽章 / 切项目 toast).

    与 `/ws/projects/{id}/preannotate` 的区别:
    - 后者是单项目: 工作台跑预标时实时帧 (current/total)
    - 本端点是全局: 仅在 job 开始 / 结束 / 失败 3 时点带 job_meta 推一条
      (project_id / project_name / job_id / status), 让前端跨项目可见

    鉴权: 仅 super_admin / project_admin (与 /ai-pre 主页一致), 其他角色直接 close.
    Channel: redis pub/sub `global:prediction-jobs` (prediction_progress.publish_progress
    includes `job_meta` on this channel).
    """
    try:
        actor = await _authenticate_socket_token(token, allow_api_key=False)
        if actor is None or actor.role not in _ADMIN_SOCKET_ROLES:
            raise PermissionError("not admin")
        user_id = str(actor.actor_id)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    channel = "global:prediction-jobs"
    await pubsub.subscribe(channel)
    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            revalidate=lambda: _revalidate_admin_stream(token, allow_api_key=False),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("prediction-jobs WS error user=%s err=%s", user_id, e)
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass


@router.websocket("/ws/video-tracker-jobs/{job_id}")
async def video_tracker_job_socket(
    websocket: WebSocket,
    job_id: uuid.UUID,
    token: str = Query(...),
):
    task_id_for_revalidate: uuid.UUID | None = None
    try:
        actor = await _authenticate_socket_token(token, allow_api_key=False)
        if actor is None:
            raise PermissionError("invalid credential")
        user_uuid = actor.actor_id
        from app.services import task_access
        from app.db.base import async_session

        async with async_session() as db:
            user = await IdentityAccountLockData(db).read_actor_status(user_uuid)
            job = await get_core_row(db, video_tracker_job_row, job_id)
            task = (
                await get_core_row(db, task_row, job.task_id)
                if job is not None
                else None
            )
            if user is None or not user.is_active or job is None or task is None:
                raise ValueError("tracker job not visible")
            await task_access.assert_task_visible(db, task, user.id)
            task_id_for_revalidate = task.id
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    channel = f"video-tracker-job:{job_id}"
    await pubsub.subscribe(channel)
    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            revalidate=lambda: _revalidate_task_stream(token, task_id_for_revalidate),
        )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass


@router.websocket("/ws/ml-backend-stats")
async def ml_backend_stats_socket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """实时推送推理服务统计，仅允许当前管理员通过 JWT 或 API key 订阅。"""
    user_id: str | None = None
    try:
        actor = await _authenticate_socket_token(token, allow_api_key=True)
        if actor is None or actor.role not in _ADMIN_SOCKET_ROLES:
            raise PermissionError("not admin")
        user_id = str(actor.actor_id)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    presence = MLStatsPresence(r)
    try:
        await pubsub.subscribe(ML_STATS_CHANNEL)
        await presence.refresh()
        await _run_pubsub_ws(
            websocket,
            pubsub,
            revalidate=lambda: _revalidate_admin_stream(token, allow_api_key=True),
            maintain=presence.maintain,
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ml-backend-stats WS error user=%s err=%s", user_id, e)
    finally:
        await presence.detach()
        try:
            await pubsub.aclose()
        except Exception:
            pass


@router.websocket("/ws/notifications")
async def notifications_socket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """Subscribe to the current user's Notifications through the owner gate."""

    await serve_notification_socket(
        websocket,
        token,
        authenticate=_notification_authenticate,
        active_actor=_load_active_user,
        open_delivery=_open_notification_delivery,
        redis_client=lambda: aioredis.Redis(
            connection_pool=_get_notification_redis_pool()
        ),
        relay=_run_pubsub_ws,
    )
