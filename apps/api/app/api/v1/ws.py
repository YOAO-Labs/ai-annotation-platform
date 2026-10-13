import asyncio
import json
import logging
import uuid
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.config import settings
from app.core.security import decode_access_token
from app.core.token_blacklist import get_user_generation, is_blacklisted
from app.db.enums import PlatformRole
from app.services.notification import channel_for

router = APIRouter()
log = logging.getLogger(__name__)

#: Administrative socket channels are limited to these *current* platform roles.
_ADMIN_SOCKET_ROLES = (
    PlatformRole.SUPER_ADMIN.value,
    PlatformRole.PROJECT_ADMIN.value,
)

# Pre-commit publishers (worker jobs) emit their Redis message before the
# notification row is committed; the per-message gate retries this long for the
# row to become visible before failing closed.
_NOTIFICATION_GATE_RETRY_SECONDS = 0.15


async def _load_active_user(user_id: str | None):
    """Resolve the current database account for a socket handshake.

    Socket tokens are long-lived: a JWT ``role`` claim can outlive a role
    change or account disable.  Restricted subscriptions must therefore
    re-resolve the account from the database instead of trusting the claim.
    Returns ``None`` when the account is missing or inactive.
    """

    if not user_id:
        return None
    from app.db.base import async_session
    from app.db.models.user import User

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return None
    async with async_session() as db:
        user = await db.get(User, user_uuid)
        if user is None or not user.is_active:
            return None
        return user


async def _authenticate_socket_token(token: str):
    """Resolve a socket token (JWT or ``ak_`` API key) to an active account."""

    from app.services import api_key_service

    if api_key_service.is_api_key_token(token):
        from app.db.base import async_session

        async with async_session() as db:
            resolved = await api_key_service.resolve_token(db, token)
            if resolved is None:
                return None
            _key, user = resolved
            await db.commit()  # 持久化 last_used_at
            return user if user.is_active else None
    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    # Mirror get_current_user: a blacklisted jti or a bumped account generation
    # invalidates the credential even though the user row is still active.
    jti: str | None = payload.get("jti")
    token_gen: int = payload.get("gen", 0)
    if jti:
        try:
            if await is_blacklisted(jti):
                return None
        except Exception:
            pass
        try:
            if await get_user_generation(user_id) > token_gen:
                return None
        except Exception:
            pass
    return await _load_active_user(user_id)


async def _revalidate_project_stream(token: str, project_id: uuid.UUID) -> bool:
    user = await _authenticate_socket_token(token)
    if user is None:
        return False
    return await _revalidate_project_access(project_id, str(user.id))


async def _notification_message_allowed(token: str, data) -> bool:
    """Per-message gate for the per-user notification socket.

    A row already published to Redis is re-resolved against the recipient's
    current project access before it is forwarded; restricted rows whose access
    was revoked are dropped.  Only the explicit ``notifications.sync`` control
    frame is allowed without a verifiable row id; malformed or unidentifiable
    payloads fail closed.
    """

    user = await _authenticate_socket_token(token)
    if user is None:
        return False
    try:
        raw = data.decode() if isinstance(data, bytes) else data
        message = json.loads(raw)
    except Exception:
        return False
    if not isinstance(message, dict):
        return False
    if message.get("type") == "notifications.sync":
        return True
    raw_id = message.get("id")
    if not raw_id:
        return False
    try:
        notification_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
        return False
    from app.db.base import async_session
    from app.db.models.notification import Notification
    from app.services.notification import NotificationService

    async with async_session() as db:
        service = NotificationService(db)
        # A notification row published before its creating transaction commits is
        # briefly invisible to this separate session (READ COMMITTED); that must
        # not be mistaken for a revoked delivery or the one real-time push is
        # dropped.  Retry briefly while the row is missing; once the row is
        # visible the deliverability answer is authoritative and still fails
        # closed on revocation or bogus ids.
        for attempt in range(4):
            if await db.get(Notification, notification_id) is not None:
                return await service.notification_deliverable(notification_id, user.id)
            if attempt < 3:
                await asyncio.sleep(_NOTIFICATION_GATE_RETRY_SECONDS)
        return False


async def _revalidate_task_stream(token: str, task_id: uuid.UUID) -> bool:
    user = await _authenticate_socket_token(token)
    if user is None:
        return False
    return await _revalidate_task_visible(task_id, str(user.id))


async def _revalidate_admin_stream(token: str) -> bool:
    user = await _authenticate_socket_token(token)
    return user is not None and user.role in _ADMIN_SOCKET_ROLES


async def _revalidate_project_access(project_id: uuid.UUID, user_id: str) -> bool:
    """Re-resolve current project access for an ongoing restricted stream.

    Project removal must stop future messages for that project while unrelated
    authorized sockets keep working.
    """

    from app.db.base import async_session
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.services.project_access import resolve_project_access

    async with async_session() as db:
        project = await db.get(Project, project_id)
        user = await db.get(User, uuid.UUID(str(user_id)))
        if project is None or user is None:
            return False
        try:
            await resolve_project_access(db, user=user, project=project)
        except Exception:
            return False
        return True


async def _revalidate_task_visible(task_id: uuid.UUID, user_id: str) -> bool:
    from app.api.v1.tasks._shared import _assert_task_visible
    from app.db.base import async_session
    from app.db.models.task import Task

    user = await _load_active_user(user_id)
    if user is None:
        return False
    async with async_session() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return False
        try:
            await _assert_task_visible(db, task, user)
        except Exception:
            return False
        return True


# v0.7.0：模块级共享连接池，避免每次 WS 连接都新建 Redis socket。
# WS 副本数 ↑ 时 Redis 连接数受 max_connections 上限保护。
_REDIS_POOL: ConnectionPool | None = None


def _get_redis_pool() -> ConnectionPool:
    global _REDIS_POOL
    if _REDIS_POOL is None:
        _REDIS_POOL = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=200,
            decode_responses=False,
        )
    return _REDIS_POOL


async def close_redis_pool() -> None:
    """v0.9.13 · uvicorn --reload / SIGTERM 时释放 Redis 连接池, 让悬挂 WS 收到 1006.

    现行架构无内存级 connection 表 (全靠 Redis pub/sub), 各 endpoint finally 块虽然
    会 close pubsub, 但 SIGTERM 路径下 finally 不一定执行 — disconnect 释放 socket,
    客户端 WS 收到 abnormal closure 后走自带的指数退避重连.

    重要: 必须 timeout 兜底. inuse_connections=True 会等持有 in-use 连接的 task 主动
    释放, 但 lifespan shutdown 阶段那些 task 未必已被 cancel (asyncio task 取消是
    协作式的); 不带 timeout 会让 worker 永久卡 "Waiting for background tasks to
    complete". 实测 2s 足够 pubsub.listen() 协程注意到 cancellation 退出.
    """
    global _REDIS_POOL
    if _REDIS_POOL is None:
        return
    try:
        await asyncio.wait_for(
            _REDIS_POOL.disconnect(inuse_connections=True),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        # 超时或任何异常都不能阻塞 shutdown; 进程退出后内核会回收 socket
        log.debug("close_redis_pool: %s (continuing shutdown)", e)
    finally:
        _REDIS_POOL = None


HEARTBEAT_INTERVAL = 30  # 秒；防 LB / nginx idle timeout（默认 60s）主动断连


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
                # Authority or credential changed after the handshake: stop
                # before forwarding this protected payload.  The endpoint's
                # finally block unsubscribes; unrelated sockets are untouched.
                return
            data = message["data"]
            if allow_message is not None and not await allow_message(data):
                # Drop this one restricted payload (for example a notification
                # whose project access was revoked after publication) while
                # keeping unrelated deliveries flowing.
                continue
            await websocket.send_text(
                data.decode() if isinstance(data, bytes) else data
            )

    async def _watch_disconnect() -> None:
        # 不期望客户端→服务端帧; await receive() 仅用于感知客户端断开 / 服务端关闭。
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

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # 把最先完成的任务里的真实错误抛给 caller (用于各 endpoint 的 log.warning);
    # 正常断连 (_watch_disconnect 返回) / 取消不算错误。
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

    user = await _authenticate_socket_token(token)
    if user is None or not await _revalidate_project_access(project_id, str(user.id)):
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
    """v0.9.13 · 项目级 batch 状态变更广播.

    Channel: `project:{project_id}:batch` (BatchService.transition / check_auto_transitions
    在 publish_batch_status_change() 推 batch.status_changed 事件). 前端
    useBatchEventsSocket 收到后 invalidate ["batches", projectId], 让标注员/admin
    多端实时看到 batch 状态翻转 (B-15).

    Handshake contract (B3 -> B4): connect with ``?token=<JWT or ak_ api_key>``.
    Project access is required at handshake and re-checked while streaming so a
    removed member stops receiving this project's events.
    """

    user = await _authenticate_socket_token(token)
    if user is None or not await _revalidate_project_access(project_id, str(user.id)):
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
    """v0.9.8 · 全局 prediction job 进度通道 (Topbar 徽章 / 切项目 toast).

    与 `/ws/projects/{id}/preannotate` 的区别:
    - 后者是单项目: 工作台跑预标时实时帧 (current/total)
    - 本端点是全局: 仅在 job 开始 / 结束 / 失败 3 时点带 job_meta 推一条
      (project_id / project_name / job_id / status), 让前端跨项目可见

    鉴权: 仅 super_admin / project_admin (与 /ai-pre 主页一致), 其他角色直接 close.
    Channel: redis pub/sub `global:prediction-jobs` (worker `_publish_progress`
    带 job_meta 时同时发到此通道).
    """
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("missing sub")
        user = await _load_active_user(user_id)
        if user is None or user.role not in _ADMIN_SOCKET_ROLES:
            raise PermissionError("not admin")
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
            revalidate=lambda: _revalidate_admin_stream(token),
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
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("missing sub")
        user_uuid = uuid.UUID(user_id)
        from app.api.v1.tasks import _assert_task_visible
        from app.db.base import async_session
        from app.db.models.task import Task
        from app.db.models.user import User
        from app.db.models.video_tracker_job import VideoTrackerJob

        async with async_session() as db:
            user = await db.get(User, user_uuid)
            job = await db.get(VideoTrackerJob, job_id)
            task = await db.get(Task, job.task_id) if job is not None else None
            if user is None or not user.is_active or job is None or task is None:
                raise ValueError("tracker job not visible")
            await _assert_task_visible(db, task, user)
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


_ML_STATS_CHANNEL = "ml-backend-stats:global"
_ML_STATS_SUBSCRIBERS_KEY = "ml-backend-stats:subscribers"


@router.websocket("/ws/ml-backend-stats")
async def ml_backend_stats_socket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """v0.9.11 PerfHud · admin-only ML backend 实时统计 WS.

    Channel: `ml-backend-stats:global` (Celery beat `publish_ml_backend_stats` 每 1s
    pull 所有 is_active=true backend 的 /health → publish snapshot list).
    订阅者计数键: `ml-backend-stats:subscribers` (INCR/DECR), beat 任务读这个
    决定是否实拉 — 0 订阅者时直接 skip, 节省 GPU 探活成本.

    鉴权: super_admin / project_admin 才能看 (运维向, 标注员不需要).
    v0.15.12 · 除 JWT 外也接受 ak_ api_key (SDK/TUI 用), role 校验不变.
    """
    user_id: str | None = None
    actor = None
    try:
        from app.services import api_key_service

        if api_key_service.is_api_key_token(token):
            # ak_ 路径: 解析 api_key → 以当前数据库账号状态/角色为准
            from app.db.base import async_session

            async with async_session() as db:
                resolved = await api_key_service.resolve_token(db, token)
                if resolved is None:
                    raise PermissionError("invalid api key")
                _key, user = resolved
                user_id = str(user.id)
                actor = user if user.is_active else None
                await db.commit()  # 持久化 last_used_at
        else:
            payload = decode_access_token(token)
            user_id = payload.get("sub")
            if not user_id:
                raise ValueError("missing sub")
            actor = await _load_active_user(user_id)
        if actor is None or actor.role not in _ADMIN_SOCKET_ROLES:
            raise PermissionError("not admin")
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    await pubsub.subscribe(_ML_STATS_CHANNEL)
    # v0.9.11 · 订阅者计数 +1 (Celery beat 1s 实拉门控)
    try:
        await r.incr(_ML_STATS_SUBSCRIBERS_KEY)
    except Exception as e:
        log.warning("incr subscribers key failed: %s", e)

    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            revalidate=lambda: _revalidate_admin_stream(token),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ml-backend-stats WS error user=%s err=%s", user_id, e)
    finally:
        try:
            # 计数 -1; 防计数漂移 (异常退出场景), 取 max(0, ...)
            count = await r.decr(_ML_STATS_SUBSCRIBERS_KEY)
            if count is not None and count < 0:
                await r.set(_ML_STATS_SUBSCRIBERS_KEY, 0)
        except Exception as e:
            log.debug("decr subscribers key failed: %s", e)
        try:
            await pubsub.unsubscribe(_ML_STATS_CHANNEL)
            await pubsub.close()
        except Exception:
            pass


@router.websocket("/ws/notifications")
async def notifications_socket(
    websocket: WebSocket,
    token: str = Query(...),
):
    """v0.6.9 · 单用户通知 WS：握手时校验 JWT，订阅 notify:{user_id}。

    DB 已持久化通知行；WS 用于在线推送，断线兜底走 30s 轮询。
    v0.7.0：使用共享 Redis ConnectionPool + 30s 心跳。
    """
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("missing sub")
        # Re-resolve the account: a disabled account with an unexpired token must
        # not keep receiving restricted per-user deliveries.
        if await _load_active_user(user_id) is None:
            raise PermissionError("inactive account")
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    r = aioredis.Redis(connection_pool=_get_redis_pool())
    pubsub = r.pubsub()
    channel = channel_for(user_id)
    await pubsub.subscribe(channel)
    try:
        await _run_pubsub_ws(
            websocket,
            pubsub,
            allow_message=lambda data: _notification_message_allowed(token, data),
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("notifications WS error user=%s err=%s", user_id, e)
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        except Exception:
            pass
