"""Atomic, durable installation drain owner and externally proven settlement."""

from aap_inference.gpu.ledger.scripts.lua_sources import _LEDGER_INTEGRITY_LUA

_INSTALLATION_DRAIN_LUA = _LEDGER_INTEGRITY_LUA + r"""
local action, resource, backend, epoch, source, generation, owner =
  ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6], ARGV[7]
local ledger, status = inspect_ledger(
  resource, ARGV[8], ARGV[9], ARGV[11], ARGV[12],
  ARGV[13], ARGV[14], ARGV[10], false, backend)
if not ledger then return cjson.encode({status=status}) end
if not ledger.memberships[backend]
   or ledger.memberships[backend].membership_epoch ~= epoch then
  return cjson.encode({status='config_mismatch'})
end
local transition = ledger.transition
local allocation = ledger.allocations[backend]
local leases = redis.call('HLEN', ledger.lease_keys[backend])
local function response(result, idempotent)
  return cjson.encode({status=result, lease_count=leases, idempotent=idempotent or false})
end
local function greater(a, b)
  return #a > #b or (#a == #b and a > b)
end
local function matches(t)
  return t and t.operation == 'installation_drain'
    and t.resource_id == resource and t.backend_id == backend
    and t.owner_id == owner and t.generation == generation
    and t.installation_membership_epoch == epoch
end
local receipt_field = 'installation_drain_receipt:' .. backend .. ':' .. owner
if action == 'begin' then
  if transition then
    if not matches(transition) or transition.installation_source_generation ~= source then
      return response('busy')
    end
    return response(leases > 0 and 'active_leases' or 'owned', true)
  end
  local receipt = integrity_decode(redis.call('HGET', KEYS[1], receipt_field))
  if matches(receipt) and receipt.installation_source_generation == source then
    return response('released', true)
  end
  local actual = allocation and allocation.generation or cjson.null
  if actual == cjson.null then actual = '' end
  if allocation and actual ~= source then return response('stale_generation') end
  if source ~= '' and not greater(generation, source) then
    return response('stale_generation')
  end
  if not integrity_has_revision_headroom(redis.call('HGET', KEYS[1], 'ledger_revision')) then
    return response('not_ready')
  end
  local t = {
    resource_id=resource, backend_id=backend, owner_id=owner,
    generation=generation, operation='installation_drain', require_idle=false,
    created_at_ms=ledger.now, expires_at_ms=ledger.now,
    installation_membership_epoch=epoch, installation_source_generation=source,
    installation_had_allocation=(allocation ~= nil), installation_settled=false
  }
  local encoded = cjson.encode(t)
  redis.call('SET', KEYS[4], encoded)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded, 'updated_at_ms', tostring(ledger.now))
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  return response(leases > 0 and 'active_leases' or 'owned')
end
if action == 'release' then
  local receipt = integrity_decode(redis.call('HGET', KEYS[1], receipt_field))
  if matches(receipt) then return response('released', true) end
end
if not transition then return response('missing') end
if not matches(transition) then return response('owner_mismatch') end
if leases > 0 then return response('active_leases') end
if not integrity_has_revision_headroom(redis.call('HGET', KEYS[1], 'ledger_revision')) then
  return response('not_ready')
end
if action == 'finish' then
  if transition.installation_settled == true then
    if allocation and (allocation.state ~= 'unloaded' or allocation.generation ~= generation) then
      return response('invalid_transition')
    end
    return response('settled', true)
  end
  local actual = allocation and allocation.generation or cjson.null
  if actual == cjson.null then actual = '' end
  if allocation and actual ~= transition.installation_source_generation then
    return response('stale_generation')
  end
  local committed = ledger.committed
  if (allocation ~= nil) ~= transition.installation_had_allocation then
    return response('invalid_transition')
  end
  if allocation then
    if integrity_counted_states[allocation.state] then committed = committed - allocation.budget_mb end
    allocation.state = 'unloaded'
    allocation.generation = generation
    allocation.evictable = false
    allocation.not_evict_before_ms = 0
    allocation.reservation_lease_id = nil
    allocation.reservation_owner_id = nil
    allocation.last_used_at_ms = math.max(allocation.last_used_at_ms, ledger.now)
    redis.call('HSET', KEYS[2], backend, cjson.encode(allocation))
  end
  transition.installation_settled = true
  local encoded = cjson.encode(transition)
  redis.call('SET', KEYS[4], encoded)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded,
    'committed_mb', tostring(committed), 'updated_at_ms', tostring(ledger.now))
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  return response('settled')
elseif action == 'release' then
  if transition.installation_settled ~= true
     or (allocation and (allocation.state ~= 'unloaded' or allocation.generation ~= generation)) then
    return response('invalid_transition')
  end
  redis.call('DEL', KEYS[4])
  redis.call('HSET', KEYS[1], 'transition_mirror', '',
    receipt_field, cjson.encode(transition), 'updated_at_ms', tostring(ledger.now))
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  return response('released')
end
return redis.error_reply('unsupported installation drain operation')
"""
