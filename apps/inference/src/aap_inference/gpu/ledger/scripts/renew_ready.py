"""Atomic, state-preserving readiness continuity CAS."""

_RENEW_READY_LUA = r"""
local card = KEYS[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local current = tonumber(redis.call('HGET', card, 'reconcile_deadline_ms'))
local proposed = tonumber(ARGV[6])
if redis.call('HGET', card, 'resource_id') ~= ARGV[1]
  or redis.call('HGET', card, 'allocatable_mb') ~= ARGV[2]
  or redis.call('HGET', card, 'ledger_revision') ~= ARGV[3]
  or redis.call('HGET', card, 'ledger_incarnation') ~= ARGV[4]
  or redis.call('HGET', card, 'reconcile_deadline_ms') ~= ARGV[5]
  or redis.call('HGET', card, 'bootstrap_state') ~= 'ready'
  or redis.call('HGET', card, 'proof_reset_state') == 'prepared'
  or redis.call('HGET', card, 'backend_domain') ~= ARGV[7]
  or redis.call('HGET', card, 'backend_domain_fingerprint') ~= ARGV[8]
  or redis.call('HGET', card, 'membership_domain') ~= ARGV[9]
  or redis.call('HGET', card, 'membership_domain_fingerprint') ~= ARGV[10]
  or redis.call('HGET', card, 'active_backend_domain') ~= ARGV[11]
  or redis.call('HGET', card, 'active_backend_domain_fingerprint') ~= ARGV[12]
  or not current or current <= now or current > now + 300000
  or not proposed or proposed <= now or proposed > now + 300000
  or tonumber(ARGV[3]) >= 9007199252740991 then
  return 0
end
if proposed > current then
  redis.call('HINCRBY', card, 'ledger_revision', 1)
  redis.call('HSET', card, 'reconcile_deadline_ms', tostring(proposed))
end
return 1
"""
