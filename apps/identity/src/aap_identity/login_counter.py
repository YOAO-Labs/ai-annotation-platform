"""Fail-open Redis login-failure counter using the existing shared key namespace."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)
_KEY_PREFIX = "login_failed:"


class RedisLoginCounter:
    def __init__(self, url: str, window_seconds: int = 3600):
        if type(window_seconds) is not int or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        self.url = url
        self.window_seconds = window_seconds

    def _client(self):
        import redis.asyncio as redis

        return redis.from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    @staticmethod
    def _log_failure(operation: str, error: Exception) -> None:
        logger.warning(
            "login_failed_counter.%s redis error_class=%s",
            operation,
            type(error).__name__,
        )

    async def _close(self, client) -> None:
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                self._log_failure("close", error)

    async def get_count(self, ip: str) -> int:
        if not ip:
            return 0
        client = None
        try:
            client = self._client()
            value = await client.get(f"{_KEY_PREFIX}{ip}")
            if value is None:
                return 0
            if type(value) is int:
                count = value
            elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                count = int(value)
            else:
                raise ValueError("Invalid login-failure counter")
            if count < 0:
                raise ValueError("Invalid login-failure counter")
            return count
        except Exception as error:
            self._log_failure("get", error)
            return 0
        finally:
            await self._close(client)

    async def increment(self, ip: str) -> int:
        if not ip:
            return 0
        client = None
        try:
            client = self._client()
            key = f"{_KEY_PREFIX}{ip}"
            count = await client.incr(key)
            if type(count) is not int or count < 0:
                raise ValueError("Invalid login-failure counter")
            if count == 1:
                await client.expire(key, self.window_seconds)
            return count
        except Exception as error:
            self._log_failure("increment", error)
            return 0
        finally:
            await self._close(client)

    async def reset(self, ip: str) -> None:
        if not ip:
            return
        client = None
        try:
            client = self._client()
            await client.delete(f"{_KEY_PREFIX}{ip}")
        except Exception as error:
            self._log_failure("reset", error)
        finally:
            await self._close(client)
