import asyncio
import logging
import uuid
from typing import cast

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

ML_STATS_CHANNEL = "ml-backend-stats:global"
_LEASES_KEY = "ml-backend-stats:leases"
_LEASE_MILLISECONDS = 30_000
_RENEW_SECONDS = 10
_COMMAND_TIMEOUT_SECONDS = 2

_REDIS_CLOCK = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
"""
_TOUCH_LEASE = (
    _REDIS_CLOCK
    + """
local expires = now + tonumber(ARGV[2])
if ARGV[3] == 'renew' then
  local previous = redis.call('ZSCORE', KEYS[1], ARGV[1])
  if not previous or tonumber(previous) <= now then
    return 0
  end
end
redis.call('ZADD', KEYS[1], expires, ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""
)
_COUNT_LIVE_LEASES = (
    _REDIS_CLOCK
    + """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""
)

log = logging.getLogger(__name__)


def count_live_stats_subscribers(redis: Redis) -> int:
    return cast(int, redis.eval(_COUNT_LIVE_LEASES, 1, _LEASES_KEY))


class MLStatsPresence:
    def __init__(self, redis: AsyncRedis):
        self._redis = redis
        self._id = uuid.uuid4().hex
        self._registered = False
        self._closed = False

    async def refresh(self) -> None:
        if self._closed:
            return
        try:
            if self._registered:
                if await self._touch("renew"):
                    return
                self._registered = False
                self._id = uuid.uuid4().hex
            self._registered = await self._touch("attach")
        except (RedisError, TimeoutError) as exc:
            log.warning("ML stats subscriber lease refresh failed: %s", exc)

    async def _touch(self, operation: str) -> bool:
        async with asyncio.timeout(_COMMAND_TIMEOUT_SECONDS):
            return bool(
                await self._redis.eval(
                    _TOUCH_LEASE,
                    1,
                    _LEASES_KEY,
                    self._id,
                    _LEASE_MILLISECONDS,
                    operation,
                )
            )

    async def maintain(self) -> None:
        while not self._closed:
            await asyncio.sleep(_RENEW_SECONDS)
            await self.refresh()

    async def detach(self) -> None:
        self._closed = True
        if not self._registered:
            return
        self._registered = False
        try:
            async with asyncio.timeout(_COMMAND_TIMEOUT_SECONDS):
                await self._redis.zrem(_LEASES_KEY, self._id)
        except (RedisError, TimeoutError) as exc:
            log.warning("ML stats subscriber lease detach failed: %s", exc)
