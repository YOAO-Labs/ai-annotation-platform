"""Process-local ownership for verified video stages and scoped native leases.

Authorization and capture belong to the caller. No stage is built by a media
GET, and no reservation is released before its actual capture/read/close ends.
"""

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
import time
import uuid

from app.services.content_snapshot import ContentError
from app.services.managed_original_reader import BinaryStage
from app.services.storage_access import mark_unsettled

MAX_STAGE_BYTES = 5 * 1024**3
MAX_STAGES = 2
MAX_LEASES_PER_STAGE = 32
MAX_RESPONSE_PINS = 8
LEASE_SECONDS = 300
STAGE_SECONDS = 3600
REAPER_SECONDS = 30


def _now():
    return datetime.now(timezone.utc)


def _error(code="video_playback_expired", status=410):
    raise ContentError(code, status)


@dataclass(frozen=True)
class Lease:
    id: uuid.UUID
    secret: str = field(repr=False)
    context: object = field(repr=False)
    expires_at: datetime
    media_type: str


@dataclass
class Pin:
    stage: BinaryStage = field(repr=False)
    context: object = field(repr=False)
    media_type: str
    expires_at: datetime
    lease_id: uuid.UUID
    _entry: object = field(repr=False)
    _registry: object = field(repr=False)
    _lease: Lease = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)


@dataclass
class _LeaseRecord:
    lease: Lease
    deadline: float
    entry: object = field(repr=False)


@dataclass
class _Entry:
    id: uuid.UUID
    key: Hashable = field(repr=False)
    context: object = field(repr=False)
    size_bytes: int
    media_type: str
    expires_at: datetime
    deadline: float
    leases: set = field(default_factory=set)
    capture: asyncio.Task | None = field(default=None, repr=False)
    stage: BinaryStage | None = field(default=None, repr=False)
    closing: asyncio.Task | None = field(default=None, repr=False)
    pins: int = 0
    published: bool = False
    retired: bool = False
    unsettled: bool = False


async def _drain(pending):
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        return pending.result(), None, cancelled
    except BaseException as exc:
        return None, exc, cancelled


class PlaybackRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _now,
        timer: Callable[[], float] = time.monotonic,
        max_stage_bytes=MAX_STAGE_BYTES,
        max_stages=MAX_STAGES,
        max_leases=MAX_LEASES_PER_STAGE,
        max_pins=MAX_RESPONSE_PINS,
        lease_seconds=LEASE_SECONDS,
        stage_seconds=STAGE_SECONDS,
        reaper_seconds=REAPER_SECONDS,
    ):
        self._clock, self._timer = clock, timer
        self._max_stage_bytes, self._max_stages = max_stage_bytes, max_stages
        self._max_leases, self._max_pins = max_leases, max_pins
        self._lease_seconds, self._stage_seconds = lease_seconds, stage_seconds
        self._reaper_seconds = reaper_seconds
        self._entries: dict[uuid.UUID, _Entry] = {}
        self._by_key: dict[Hashable, _Entry] = {}
        self._leases: dict[uuid.UUID, _LeaseRecord] = {}
        self._pins = 0
        self._active_pins: dict[int, Pin] = {}
        self._closed = False
        self._changed = asyncio.Event()
        self._reaper = None
        self._shutdown = None

    def _forget(self, entry):
        self._entries.pop(entry.id, None)
        if self._by_key.get(entry.key) is entry:
            self._by_key.pop(entry.key)
        self._changed.set()

    async def _close(self, entry):
        try:
            # The private close task is never cancelled: callers drain it, even
            # when repeatedly cancelled themselves. Unknown close retains quota.
            context = contextvars.copy_context()
            await asyncio.get_running_loop().run_in_executor(
                None, context.run, entry.stage.close
            )
        except BaseException:
            entry.unsettled = True
        else:
            self._forget(entry)
        finally:
            self._changed.set()

    def _cleanup(self, entry):
        if (
            not entry.retired
            or entry.pins
            or entry.unsettled
            or (entry.capture is not None and not entry.capture.done())
        ):
            return
        if entry.stage is None:
            self._forget(entry)
        elif entry.closing is None:
            entry.closing = asyncio.create_task(self._close(entry))

    def _retire(self, entry):
        entry.retired = True
        if self._by_key.get(entry.key) is entry:
            self._by_key.pop(entry.key)
        for identifier in tuple(entry.leases):
            self._leases.pop(identifier, None)
        entry.leases.clear()
        self._cleanup(entry)
        self._changed.set()

    def _expire(self):
        now, tick = self._clock(), self._timer()
        for identifier, record in tuple(self._leases.items()):
            if record.lease.expires_at <= now or record.deadline <= tick:
                self._leases.pop(identifier, None)
                record.entry.leases.discard(identifier)
        for entry in tuple(self._entries.values()):
            ready = entry.capture is not None and entry.capture.done()
            if (
                entry.expires_at <= now
                or entry.deadline <= tick
                or (ready and entry.published and not entry.leases)
            ):
                self._retire(entry)
        self._changed.set()

    async def _wait_close(self, entry):
        self._cleanup(entry)
        if entry.closing is not None:
            _, failure, cancelled = await _drain(entry.closing)
            if failure is not None:
                entry.unsettled = True
            if cancelled:
                error = asyncio.CancelledError()
                if entry.unsettled:
                    mark_unsettled(error)
                raise error

    async def _capture(self, entry, factory):
        try:
            stage = await factory()
            if stage is None:
                _error("video_playback_capture_incomplete", 503)
            entry.stage = stage
            if stage.size != entry.size_bytes:
                _error("content_mismatch", 409)
            return stage
        except BaseException as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                entry.unsettled = True
            self._retire(entry)
            raise
        finally:
            self._changed.set()

    def _lease(self, entry, expires_at):
        now, tick = self._clock(), self._timer()
        duration = min(
            self._lease_seconds,
            (expires_at - now).total_seconds(),
            (entry.expires_at - now).total_seconds(),
            entry.deadline - tick,
        )
        if duration <= 0:
            _error()
        if len(entry.leases) >= self._max_leases:
            _error("video_playback_lease_capacity_exceeded", 429)
        lease = Lease(
            uuid.uuid4(),
            secrets.token_urlsafe(32),
            entry.context,
            now + timedelta(seconds=duration),
            entry.media_type,
        )
        self._leases[lease.id] = _LeaseRecord(lease, tick + duration, entry)
        entry.leases.add(lease.id)
        entry.published = True
        return lease

    async def prepare(
        self,
        key: Hashable,
        context: object,
        size_bytes: int,
        factory: Callable[[], Awaitable[BinaryStage]],
        expires_at: datetime,
        media_type: str,
    ) -> Lease:
        try:
            hash(key)
            valid_expiry = (
                isinstance(expires_at, datetime) and expires_at.utcoffset() is not None
            )
        except (TypeError, ValueError, OverflowError):
            valid_expiry = False
        if (
            not valid_expiry
            or type(size_bytes) is not int
            or size_bytes < 0
            or not isinstance(media_type, str)
            or not media_type
        ):
            _error("video_playback_invalid", 422)
        if size_bytes > self._max_stage_bytes:
            _error("content_too_large", 413)
        if self._closed:
            _error("video_playback_unavailable", 503)
        if expires_at <= self._clock():
            _error()
        await self.reap()
        if self._closed:
            _error("video_playback_unavailable", 503)
        entry = self._by_key.get(key)
        if entry is not None:
            if entry.capture is not None and not entry.capture.done():
                _error("video_playback_preparing", 429)
            if (
                entry.context != context
                or entry.size_bytes != size_bytes
                or entry.media_type != media_type
            ):
                _error("video_playback_context_changed", 409)
            return self._lease(entry, expires_at)
        if len(self._entries) >= self._max_stages:
            _error("video_playback_capacity_exceeded", 429)
        now, tick = self._clock(), self._timer()
        entry = _Entry(
            uuid.uuid4(),
            key,
            context,
            size_bytes,
            media_type,
            now + timedelta(seconds=self._stage_seconds),
            tick + self._stage_seconds,
        )
        self._entries[entry.id] = self._by_key[key] = entry
        entry.capture = asyncio.create_task(self._capture(entry, factory))
        try:
            await asyncio.shield(entry.capture)
            if entry.retired or self._closed:
                _error()
            return self._lease(entry, expires_at)
        except BaseException as exc:
            self._retire(entry)
            if not entry.capture.done():
                entry.capture.cancel()
            _, late_failure, cancelled = await _drain(entry.capture)
            if getattr(late_failure, "storage_operation_unsettled", False):
                entry.unsettled = True
            await self._wait_close(entry)
            if isinstance(exc, asyncio.CancelledError) or cancelled:
                error = asyncio.CancelledError()
                if entry.unsettled:
                    mark_unsettled(error)
                raise error from None
            raise

    def _record(self, lease_id):
        self._expire()
        record = self._leases.get(lease_id)
        if self._closed or record is None:
            _error()
        entry = record.entry
        if (
            entry.retired
            or entry.unsettled
            or not entry.published
            or entry.stage is None
            or entry.closing is not None
            or entry.capture is None
            or not entry.capture.done()
            or self._entries.get(entry.id) is not entry
            or self._by_key.get(entry.key) is not entry
        ):
            _error()
        return record

    def validate_lease(self, lease: Lease):
        """Recheck a returned capability after an authorization await."""
        if not isinstance(lease, Lease) or not isinstance(lease.id, uuid.UUID):
            _error()
        record = self._record(lease.id)
        if record.lease is not lease:
            _error()

    def validate_pin(self, pin: Pin):
        """Keep an old FD alive without admitting a retired capability to serve."""
        if (
            not isinstance(pin, Pin)
            or pin._registry is not self
            or pin._released
            or self._active_pins.get(id(pin)) is not pin
        ):
            _error()
        self.validate_lease(pin._lease)
        record = self._record(pin.lease_id)
        if (
            record.lease is not pin._lease
            or record.entry is not pin._entry
            or record.entry.stage is not pin.stage
            or record.lease.context is not pin.context
            or record.lease.media_type != pin.media_type
            or record.lease.expires_at != pin.expires_at
        ):
            _error()

    def context_for_release(self, lease_id: uuid.UUID) -> object:
        return self._record(lease_id).lease.context

    def pin(self, lease_id: uuid.UUID, secret: str) -> Pin:
        record = self._record(lease_id)
        if (
            not isinstance(secret, str)
            or not secret.isascii()
            or len(secret) > 256
            or not secrets.compare_digest(record.lease.secret, secret)
        ):
            _error("video_playback_capability_invalid", 403)
        if self._pins >= self._max_pins:
            _error("video_playback_response_capacity_exceeded", 429)
        entry = record.entry
        self._pins += 1
        entry.pins += 1
        pin = Pin(
            entry.stage,
            record.lease.context,
            record.lease.media_type,
            record.lease.expires_at,
            lease_id,
            entry,
            self,
            record.lease,
        )
        self._active_pins[id(pin)] = pin
        return pin

    async def release_pin(self, pin: Pin):
        if not isinstance(pin, Pin) or pin._registry is not self:
            raise ValueError("Pin does not belong to this registry")
        if pin._released:
            return
        if self._active_pins.get(id(pin)) is not pin:
            raise ValueError("Pin was not acquired from this registry")
        pin._released = True
        self._active_pins.pop(id(pin))
        self._pins -= 1
        pin._entry.pins -= 1
        self._changed.set()
        await self._wait_close(pin._entry)

    async def revoke_lease(self, lease_id: uuid.UUID):
        record = self._leases.pop(lease_id, None)
        if record is None:
            return
        entry = record.entry
        entry.leases.discard(lease_id)
        if not entry.leases:
            self._retire(entry)
        await self._wait_close(entry)

    async def revoke_contexts(self, predicate: Callable[[object], bool]):
        """Revoke a snapshot of matching leases, preserving active response pins."""
        records = tuple(
            record
            for record in self._leases.values()
            if predicate(record.lease.context)
        )
        pending = asyncio.gather(
            *(self.revoke_lease(record.lease.id) for record in records),
            return_exceptions=True,
        )
        results, failure, cancelled = await _drain(pending)
        if cancelled:
            error = asyncio.CancelledError()
            if any(record.entry.unsettled for record in records):
                mark_unsettled(error)
            raise error
        if failure is not None:
            raise failure
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def reap(self):
        self._expire()
        pending = tuple(self._entries.values())
        for entry in pending:
            await self._wait_close(entry)

    def start_reaper(self):
        if self._closed:
            _error("video_playback_unavailable", 503)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._run_reaper())

    async def _run_reaper(self):
        while True:
            await asyncio.sleep(self._reaper_seconds)
            await self.reap()

    async def _shutdown_all(self):
        if self._reaper is not None:
            self._reaper.cancel()
            await _drain(self._reaper)
        for entry in tuple(self._entries.values()):
            self._retire(entry)
            if entry.capture is not None and not entry.capture.done():
                entry.capture.cancel()
        for entry in tuple(self._entries.values()):
            if entry.capture is not None:
                await _drain(entry.capture)
        while True:
            self._changed.clear()
            for entry in tuple(self._entries.values()):
                await self._wait_close(entry)
            if not any(
                entry.pins or (entry.closing is not None and not entry.closing.done())
                for entry in self._entries.values()
            ):
                return
            await self._changed.wait()

    async def shutdown(self):
        self._closed = True
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._shutdown_all())
        _, failure, cancelled = await _drain(self._shutdown)
        if cancelled:
            error = asyncio.CancelledError()
            if any(entry.unsettled for entry in self._entries.values()):
                mark_unsettled(error)
            raise error
        if failure is not None:
            raise failure

    def diagnostics(self):
        return {
            "stages": len(self._entries),
            "reserved_bytes": sum(entry.size_bytes for entry in self._entries.values()),
            "leases": len(self._leases),
            "response_pins": self._pins,
            "unsettled_stages": sum(
                entry.unsettled for entry in self._entries.values()
            ),
        }


registry = PlaybackRegistry()
