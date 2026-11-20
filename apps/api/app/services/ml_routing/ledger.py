"""ADR-0050 §9 / §B.4 · Redis routing ledger.

Atomic route-lease lifecycle independent of the GPU arbitration ledger (ADR-0050 D7).
Namespace ``ml-router:v1`` (zero overlap with ``gpu-arbiter:v1``).

The acquire Lua script runs in ONE atomic step (§9.1):
1. validate pool/candidate generation
2. sweep expired route leases
3. exclude circuit-open + max-concurrency-saturated candidates
4. smooth weighted round robin over the remainder
5. write exact lease id / pool / instance / owner / operation / expiry
6. advance SWRR current_weight + selection counter + metrics bucket
7. return selected instance or structured rejection reason

heartbeat extends an exact unexpired lease (no pool/instance/owner mutation).
finish(lease, outcome, duration) is idempotent + drives passive circuit + metrics.
cancel releases without tripping the circuit. All three are exact + idempotent;
duplicate terminal calls return the same stable result without double-counting metrics.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services.ml_routing.contracts import (
    RouteLease,
    RouteOutcome,
    RoutingCandidate,
    RejectionReason,
)

DEFAULT_NAMESPACE = "ml-router:v1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pool_state_key(pool_id: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{namespace}:pool:{pool_id}:state"


def _member_leases_key(
    pool_id: str, instance_id: str, namespace: str = DEFAULT_NAMESPACE
) -> str:
    return f"{namespace}:pool:{pool_id}:member:{instance_id}:leases"


def _lease_key(lease_id: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{namespace}:lease:{lease_id}"


def _circuit_key(
    pool_id: str, instance_id: str, namespace: str = DEFAULT_NAMESPACE
) -> str:
    return f"{namespace}:pool:{pool_id}:member:{instance_id}:circuit"


def _metrics_key(pool_id: str, bucket: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{namespace}:pool:{pool_id}:metrics:{bucket}"


def _minute_bucket(now_ms: int) -> str:
    """Fixed minute bucket (UTC) for metrics aggregation. TTL-reclaimed."""
    secs = now_ms // 1000
    return time.strftime("%Y%m%d%H%M", time.gmtime(secs))


# ── Lua scripts (frozen I/O; golden tests in tests/test_ml_routing_ledger.py) ──

# ACQUIRE: one atomic step per §9.1.
# KEYS: dynamic — we pass the pool:state key + each candidate's member:leases key.
#       KEYS[1] = pool:state, KEYS[2..] = member:{instance_id}:leases (one per candidate)
# ARGV: pool_id, candidate_generation, now_ms, lease_ttl_ms, lease_id, owner, operation,
#       bucket, then per-candidate: instance_id, weight, max_concurrency (3 ARGV each, same order as KEYS[2..])
# Returns: {1, selected_instance_id} or {0, reason_code}
_ACQUIRE_LUA = """
local pool_state = KEYS[1]
local pool_id = ARGV[1]
local candidate_gen = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local lease_id = ARGV[5]
local owner = ARGV[6]
local operation = ARGV[7]
local bucket = ARGV[8]

-- 1. generation check
-- pool:state.generation is the durable topology epoch. On first contact it's absent;
-- the caller's candidate_generation (sourced from DB) is treated as authoritative and
-- persisted here so subsequent acquires and bumps share the same baseline.
local stored_gen_raw = redis.call('HGET', pool_state, 'generation')
local stored_gen
if stored_gen_raw == false then
  redis.call('HSET', pool_state, 'generation', candidate_gen)
  stored_gen = candidate_gen
else
  stored_gen = tonumber(stored_gen_raw)
end
if candidate_gen > stored_gen then
  -- DB generation is the durable authority.  Atomically advance Redis after a
  -- missed management-plane sync and discard generation-local SWRR state.
  for _, field in ipairs(redis.call('HKEYS', pool_state)) do
    if string.sub(field, 1, 3) == 'cw:' then
      redis.call('HDEL', pool_state, field)
    end
  end
  redis.call('HDEL', pool_state, 'last_winner')
  redis.call('HSET', pool_state, 'generation', candidate_gen)
  stored_gen = candidate_gen
elseif candidate_gen < stored_gen then
  return {0, 'generation_mismatch'}
end

-- 2. parse candidates (KEYS[2..] align with ARGV[9..] triples)
local eligible = {}
local eligible_total_weight = 0
local n_candidates = #KEYS - 1
for i = 1, n_candidates do
  local leases_key = KEYS[1 + i]
  local instance_id = ARGV[8 + (i - 1) * 3 + 1]
  local weight = tonumber(ARGV[8 + (i - 1) * 3 + 2])
  local max_conc = tonumber(ARGV[8 + (i - 1) * 3 + 3])

  -- 2a. sweep expired leases for this member (crash reclaim, §C.2)
  redis.call('ZREMRANGEBYSCORE', leases_key, '-inf', now_ms)

  -- 2b. concurrency check (circuit-open members are pre-filtered by the caller)
  local inflight = redis.call('ZCARD', leases_key)
  if inflight < max_conc then
    table.insert(eligible, {instance_id, weight, leases_key})
    eligible_total_weight = eligible_total_weight + weight
  end
end

if #eligible == 0 then
  return {0, 'ml_backend_pool_saturated'}
end

-- 3. SWRR: advance current_weight, pick max (tie → smallest instance_id), subtract total.
for _, cand in ipairs(eligible) do
  local cw = tonumber(redis.call('HGET', pool_state, 'cw:' .. cand[1]) or '0')
  redis.call('HSET', pool_state, 'cw:' .. cand[1], cw + cand[2])
end
-- pick max current_weight; tie-break smallest instance_id
local winner = nil
local winner_cw = nil
local winner_leases_key = nil
for _, cand in ipairs(eligible) do
  local cw = tonumber(redis.call('HGET', pool_state, 'cw:' .. cand[1]) or '0')
  if winner == nil or cw > winner_cw or (cw == winner_cw and cand[1] < winner) then
    winner = cand[1]
    winner_cw = cw
    winner_leases_key = cand[3]
  end
end
local winner_cw_after = winner_cw - eligible_total_weight
redis.call('HSET', pool_state, 'cw:' .. winner, winner_cw_after)

-- 4. write lease
local expiry = now_ms + ttl_ms
redis.call('HSET', pool_state, 'last_winner', winner)
redis.call('HINCRBY', pool_state, 'selections:' .. winner, 1)

local lease_key = '__lease_placeholder__'
-- derive lease key from namespace embedded in pool_state key prefix
local ns_end = string.find(pool_state, ':pool:', 1, true)
local namespace = string.sub(pool_state, 1, ns_end - 1)
lease_key = namespace .. ':lease:' .. lease_id
redis.call('HSET', lease_key, 'pool', pool_id, 'instance', winner, 'owner', owner,
           'operation', operation, 'generation', candidate_gen, 'expires_at', expiry,
           'state', 'active')
redis.call('PEXPIRE', lease_key, ttl_ms)
redis.call('ZADD', winner_leases_key, expiry, lease_id)
redis.call('PEXPIRE', winner_leases_key, ttl_ms + 60000)

-- 5. metrics bucket
local metrics_key = namespace .. ':pool:' .. pool_id .. ':metrics:' .. bucket
redis.call('HINCRBY', metrics_key, 'selections', 1)
redis.call('PEXPIRE', metrics_key, 7200 * 1000)

return {1, winner}
"""

# HEARTBEAT: extend an exact unexpired lease; no pool/instance/owner mutation.
# KEYS: lease_key, member_leases_key
# ARGV: lease_id, pool_id, instance_id, owner, now_ms, new_ttl_ms
# Returns: 1 (extended) or 0 (lease missing/expired/owner mismatch)
_HEARTBEAT_LUA = """
local lease_key = KEYS[1]
local leases_key = KEYS[2]
local lease_id = ARGV[1]
local pool_id = ARGV[2]
local instance_id = ARGV[3]
local owner = ARGV[4]
local now_ms = tonumber(ARGV[5])
local new_ttl_ms = tonumber(ARGV[6])

local lease = redis.call('HMGET', lease_key, 'pool', 'instance', 'owner', 'expires_at', 'state')
if lease[1] ~= pool_id or lease[2] ~= instance_id or lease[3] ~= owner then
  return 0
end
if lease[5] ~= 'active' then
  return 0
end
local expires_at = tonumber(lease[4] or '0')
if expires_at < now_ms then
  return 0
end
local new_expiry = now_ms + new_ttl_ms
redis.call('HSET', lease_key, 'expires_at', new_expiry)
redis.call('PEXPIRE', lease_key, new_ttl_ms)
redis.call('ZADD', leases_key, new_expiry, lease_id)
redis.call('PEXPIRE', leases_key, new_ttl_ms + 60000)
return 1
"""

# FINISH: idempotent terminal; releases lease + updates circuit + metrics.
# KEYS: lease_key, member_leases_key, circuit_key, metrics_key
# ARGV: lease_id, pool_id, instance_id, owner, outcome, duration_ms, now_ms, fail_threshold, eject_ms
# Returns: 1 (released) or 2 (already-finished idempotent) or 0 (lease missing/mismatch)
_FINISH_LUA = """
local lease_key = KEYS[1]
local leases_key = KEYS[2]
local circuit_key = KEYS[3]
local metrics_key = KEYS[4]
local lease_id = ARGV[1]
local pool_id = ARGV[2]
local instance_id = ARGV[3]
local owner = ARGV[4]
local outcome = ARGV[5]
local now_ms = tonumber(ARGV[7])
local fail_threshold = tonumber(ARGV[8])
local eject_ms = tonumber(ARGV[9])

local lease = redis.call('HMGET', lease_key, 'pool', 'instance', 'owner', 'state')
if lease[4] == 'finished' or lease[4] == 'cancelled' then
  return 2  -- idempotent: already terminated
end
if lease[1] ~= pool_id or lease[2] ~= instance_id or lease[3] ~= owner then
  return 0
end

-- mark terminal + release
redis.call('HSET', lease_key, 'state', 'finished')
redis.call('ZREM', leases_key, lease_id)
-- leave lease_key with a short TTL for idempotency window then expire
redis.call('PEXPIRE', lease_key, 5000)

-- metrics
redis.call('HINCRBY', metrics_key, outcome, 1)
redis.call('PEXPIRE', metrics_key, 7200 * 1000)

-- passive circuit: only transport failures trip
if outcome == 'connect_refused' or outcome == 'transport_timeout'
   or outcome == 'no_response' or outcome == 'gateway_unavailable' then
  local fails = redis.call('HINCRBY', circuit_key, 'consecutive_failures', 1)
  if fails >= fail_threshold then
    redis.call('HSET', circuit_key, 'state', 'open', 'open_until', now_ms + eject_ms)
    redis.call('PEXPIRE', circuit_key, eject_ms + 60000)
  end
else
  -- success or any non-transport outcome resets the failure counter
  if outcome == 'success' then
    redis.call('HSET', circuit_key, 'consecutive_failures', 0, 'state', 'closed')
    redis.call('PERSIST', circuit_key)
  end
end
return 1
"""

# CANCEL: release without tripping circuit; idempotent.
# KEYS: lease_key, member_leases_key, metrics_key
# ARGV: lease_id, pool_id, instance_id, owner
# Returns: 1 (released) or 2 (already-finished idempotent) or 0 (missing/mismatch)
_CANCEL_LUA = """
local lease_key = KEYS[1]
local leases_key = KEYS[2]
local metrics_key = KEYS[3]
local lease_id = ARGV[1]
local pool_id = ARGV[2]
local instance_id = ARGV[3]
local owner = ARGV[4]

local lease = redis.call('HMGET', lease_key, 'pool', 'instance', 'owner', 'state')
if lease[4] == 'finished' or lease[4] == 'cancelled' then
  return 2
end
if lease[1] ~= pool_id or lease[2] ~= instance_id or lease[3] ~= owner then
  return 0
end
redis.call('HSET', lease_key, 'state', 'cancelled')
redis.call('ZREM', leases_key, lease_id)
redis.call('PEXPIRE', lease_key, 5000)
redis.call('HINCRBY', metrics_key, 'cancel', 1)
redis.call('PEXPIRE', metrics_key, 7200 * 1000)
return 1
"""

_SYNC_GENERATION_LUA = """
local key = KEYS[1]
local expected = tonumber(ARGV[1])
local current_raw = redis.call('HGET', key, 'generation')
local current = current_raw and tonumber(current_raw) or nil
if current ~= nil and current > expected then
  return {0, current}
end
if current == nil or current < expected then
  for _, field in ipairs(redis.call('HKEYS', key)) do
    if string.sub(field, 1, 3) == 'cw:' then
      redis.call('HDEL', key, field)
    end
  end
  redis.call('HDEL', key, 'last_winner')
  redis.call('HSET', key, 'generation', expected)
end
return {1, expected}
"""

_MEMBER_INFLIGHT_LUA = """
local leases_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', leases_key, '-inf', now_ms)
return redis.call('ZCARD', leases_key)
"""


class RoutingLedger:
    """Async Redis routing ledger client. One per event loop.

    All lease-lifecycle operations are atomic via registered Lua scripts. The caller
    (MLBackendRouter) is responsible for: building candidates from DB topology under a
    single routing_generation, pre-filtering circuit-open members, and calling
    finish()/cancel() exactly once per lease in a try/finally.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        lease_ttl_ms: int = 120_000,
        heartbeat_interval_ms: int = 15_000,
        passive_failure_threshold: int = 3,
        eject_ms: int = 30_000,
    ) -> None:
        self._redis = redis
        self.namespace = namespace
        self.lease_ttl_ms = lease_ttl_ms
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.passive_failure_threshold = passive_failure_threshold
        self.eject_ms = eject_ms
        self._acquire = redis.register_script(_ACQUIRE_LUA)
        self._heartbeat = redis.register_script(_HEARTBEAT_LUA)
        self._finish = redis.register_script(_FINISH_LUA)
        self._cancel = redis.register_script(_CANCEL_LUA)
        self._sync_generation = redis.register_script(_SYNC_GENERATION_LUA)
        self._member_inflight = redis.register_script(_MEMBER_INFLIGHT_LUA)

    @classmethod
    def from_url(cls, redis_url: str, **kwargs: Any) -> "RoutingLedger":
        client = Redis.from_url(redis_url, decode_responses=True)
        return cls(client, **kwargs)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def __aenter__(self) -> "RoutingLedger":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ── acquire ───────────────────────────────────────────────────────────────
    async def acquire(
        self,
        pool_id: str,
        generation: int,
        candidates: list[RoutingCandidate],
        *,
        owner: str,
        operation: str,
        circuit_open_instances: set[str] | None = None,
        lease_id: str | None = None,
    ) -> tuple[RouteLease | None, RejectionReason | None]:
        """Atomically acquire a route lease. Returns (lease, None) or (None, reason).

        Circuit-open and concurrency-saturated candidates are excluded atomically.
        Pre-filter circuit-open here (the Lua focuses on concurrency + SWRR) and pass
        only the remaining candidates.
        """
        circuit_open_instances = circuit_open_instances or set()
        eligible = [
            c
            for c in candidates
            if str(c.instance_id) not in circuit_open_instances
            and c.traffic_state.value == "active"
            and c.fingerprint_ok
            and c.health_fresh
        ]
        if not eligible:
            # Distinguish "no active member" from "all circuit open" for diagnostics.
            if candidates and all(
                str(c.instance_id) in circuit_open_instances for c in candidates
            ):
                return None, RejectionReason.ALL_CIRCUITS_OPEN
            return None, RejectionReason.POOL_UNAVAILABLE

        now_ms = _now_ms()
        bucket = _minute_bucket(now_ms)
        lease_id = lease_id or uuid.uuid4().hex

        # KEYS: pool:state + each eligible member's leases key
        keys = [_pool_state_key(pool_id, self.namespace)]
        keys += [
            _member_leases_key(pool_id, str(c.instance_id), self.namespace)
            for c in eligible
        ]
        # ARGV: pool_id, gen, now, ttl, lease_id, owner, operation, bucket, then triples
        argv: list[str] = [
            pool_id,
            str(generation),
            str(now_ms),
            str(self.lease_ttl_ms),
            lease_id,
            owner,
            operation,
            bucket,
        ]
        for c in eligible:
            argv += [str(c.instance_id), str(c.weight), str(c.max_concurrency)]

        try:
            result = await self._acquire(keys=keys, args=argv)
        except RedisError:
            return None, RejectionReason.ROUTER_UNAVAILABLE

        # result is a list [ok, instance_or_reason]
        ok = int(result[0])
        if ok == 1:
            instance_id = result[1]
            return (
                RouteLease(
                    lease_id=lease_id,
                    pool_id=pool_id,
                    instance_id=instance_id,
                    owner=owner,
                    operation=operation,
                    generation=generation,
                    expires_at_ms=now_ms + self.lease_ttl_ms,
                ),
                None,
            )
        # ok == 0 → rejection
        reason_str = result[1]
        if reason_str == "generation_mismatch":
            return None, RejectionReason.GENERATION_MISMATCH
        if reason_str == RejectionReason.POOL_SATURATED.value:
            return None, RejectionReason.POOL_SATURATED
        return None, RejectionReason.POOL_UNAVAILABLE

    # ── heartbeat ─────────────────────────────────────────────────────────────
    async def heartbeat(self, lease: RouteLease) -> bool:
        """Extend an unexpired exact lease. Returns True if extended."""
        keys = [
            _lease_key(lease.lease_id, self.namespace),
            _member_leases_key(lease.pool_id, lease.instance_id, self.namespace),
        ]
        argv = [
            lease.lease_id,
            lease.pool_id,
            lease.instance_id,
            lease.owner,
            str(_now_ms()),
            str(self.lease_ttl_ms),
        ]
        try:
            result = await self._heartbeat(keys=keys, args=argv)
        except RedisError:
            return False
        return int(result) == 1

    # ── finish ────────────────────────────────────────────────────────────────
    async def finish(
        self, lease: RouteLease, outcome: RouteOutcome, duration_ms: int
    ) -> bool:
        """Idempotent terminal release. Drives circuit + metrics. Returns True if this
        call performed the release (False if already finished/cancelled = idempotent no-op)."""
        keys = [
            _lease_key(lease.lease_id, self.namespace),
            _member_leases_key(lease.pool_id, lease.instance_id, self.namespace),
            _circuit_key(lease.pool_id, lease.instance_id, self.namespace),
            _metrics_key(lease.pool_id, _minute_bucket(_now_ms()), self.namespace),
        ]
        argv = [
            lease.lease_id,
            lease.pool_id,
            lease.instance_id,
            lease.owner,
            outcome.value,
            str(duration_ms),
            str(_now_ms()),
            str(self.passive_failure_threshold),
            str(self.eject_ms),
        ]
        try:
            result = await self._finish(keys=keys, args=argv)
        except RedisError:
            return False
        code = int(result)
        return code == 1  # 2 = already terminal (idempotent), 0 = mismatch

    # ── cancel ────────────────────────────────────────────────────────────────
    async def cancel(self, lease: RouteLease) -> bool:
        """Caller-initiated cancel. Never trips the circuit. Idempotent."""
        keys = [
            _lease_key(lease.lease_id, self.namespace),
            _member_leases_key(lease.pool_id, lease.instance_id, self.namespace),
            _metrics_key(lease.pool_id, _minute_bucket(_now_ms()), self.namespace),
        ]
        argv = [lease.lease_id, lease.pool_id, lease.instance_id, lease.owner]
        try:
            result = await self._cancel(keys=keys, args=argv)
        except RedisError:
            return False
        return int(result) == 1

    # ── circuit introspection (for router candidate pre-filtering) ────────────
    async def circuit_open_instances(self, pool_id: str) -> set[str]:
        """Return the set of instance_ids whose circuit is currently open in this pool.

        Used by MLBackendRouter.acquire() to pre-filter candidates before the atomic Lua.
        """
        # Circuit keys are per-member; we scan the pool's member:*:circuit keys.
        pattern = f"{self.namespace}:pool:{pool_id}:member:*:circuit"
        open_ids: set[str] = set()
        try:
            async for key in self._redis.scan_iter(match=pattern, count=100):
                state, open_until = await self._redis.hmget(key, "state", "open_until")
                if state == "open":
                    try:
                        if int(open_until or 0) > _now_ms():
                            # extract instance_id from key
                            inst = key.split(":member:")[1].split(":circuit")[0]
                            open_ids.add(inst)
                    except (ValueError, IndexError):
                        continue
        except RedisError:
            return set()
        return open_ids

    async def healthcheck(self) -> None:
        """Require a live Redis response; callers decide how to surface failure."""
        await self._redis.ping()

    async def member_inflight(self, pool_id: str, instance_id: str) -> int:
        """Return a fresh exact-lease count after atomically sweeping expirations."""
        result = await self._member_inflight(
            keys=[_member_leases_key(pool_id, instance_id, self.namespace)],
            args=[str(_now_ms())],
        )
        return int(result)

    async def sync_generation(self, pool_id: str, expected_generation: int) -> bool:
        """Monotonically synchronize Redis to the exact durable DB generation."""
        result = await self._sync_generation(
            keys=[_pool_state_key(pool_id, self.namespace)],
            args=[str(expected_generation)],
        )
        return int(result[0]) == 1

    async def bump_generation(self, pool_id: str) -> int:
        """Advance routing_generation (call when pool/member/weight/traffic changes).

        Old-generation in-flight acquires are rejected (ADR-0050 D16). Returns new gen.
        """
        key = _pool_state_key(pool_id, self.namespace)
        try:
            new_gen = await self._redis.hincrby(key, "generation", 1)
            return int(new_gen)
        except RedisError:
            return -1
