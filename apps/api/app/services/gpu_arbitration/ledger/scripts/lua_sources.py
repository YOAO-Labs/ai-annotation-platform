"""Redis Lua scripts for the GPU arbitration ledger.

Every string below is one of the ledger's atomic Redis scripts.
The composition graph (preamble + body) is preserved exactly so the 15 final scripts
registered by ``GPUArbiterStore`` produce identical SHA-256 digests. No de-duplication,
re-chunking or reordering is performed.
"""

_MARK_CARD_NOT_READY_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
if type(ARGV[5]) ~= 'string' or ARGV[5] == ''
   or string.len(ARGV[5]) > 128 then
  return cjson.encode({
    status='ledger_corrupt', reason='proposed_incarnation_invalid',
    ledger_revision=0, ledger_incarnation=''
  })
end
local existing_resource = redis.call('HGET', KEYS[1], 'resource_id')
local card_exists = redis.call('EXISTS', KEYS[1]) ~= 0
if existing_resource and existing_resource ~= ARGV[1] then
  return cjson.encode({
    status='ledger_corrupt', reason='resource_identity_mismatch',
    ledger_revision=0,
    ledger_incarnation=redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
  })
end
if card_exists and not existing_resource then
  return cjson.encode({
    status='ledger_corrupt', reason='resource_identity_missing',
    ledger_revision=0,
    ledger_incarnation=redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
  })
end
if existing_resource == ARGV[1]
   and redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
  return cjson.encode({
    status='not_ready', reason='proof_reset_in_progress',
    ledger_revision=tonumber(
      redis.call('HGET', KEYS[1], 'ledger_revision') or '0') or 0,
    ledger_incarnation=redis.call(
      'HGET', KEYS[1], 'ledger_incarnation') or '',
    committed_mb=tonumber(
      redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0
  })
end
local revision_raw = redis.call('HGET', KEYS[1], 'ledger_revision')
if card_exists and not revision_raw then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_missing')
  return cjson.encode({
    status='ledger_corrupt', reason='ledger_revision_missing',
    ledger_revision=0,
    ledger_incarnation=redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
  })
end
if revision_raw and (not string.match(revision_raw, '^[1-9][0-9]*$')
    or string.len(revision_raw) > 16
    or (string.len(revision_raw) == 16
      and revision_raw > '9007199254740991')) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_invalid')
  return cjson.encode({
    status='ledger_corrupt', reason='ledger_revision_invalid',
    ledger_revision=0,
    ledger_incarnation=redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
  })
end
local incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation')
if card_exists and (
    type(incarnation) ~= 'string' or incarnation == ''
    or string.len(incarnation) > 128) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_incarnation_invalid')
  return cjson.encode({
    status='ledger_corrupt', reason='ledger_incarnation_invalid',
    ledger_revision=tonumber(revision_raw or '0') or 0,
    ledger_incarnation=''
  })
end
if card_exists and redis.call('HGET', KEYS[1], 'ledger_version') ~= '3' then
  local revision = tonumber(revision_raw)
  if revision_raw ~= '9007199254740991' then
    revision = redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  end
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'legacy_schema_requires_proof_reset',
    'updated_at_ms', tostring(now_ms()))
  return cjson.encode({
    status='ledger_corrupt', reason='legacy_schema_requires_proof_reset',
    ledger_revision=revision, ledger_incarnation=incarnation or '',
    committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0
  })
end
if card_exists and (
    not redis.call('HGET', KEYS[1], 'backend_domain')
    or not redis.call('HGET', KEYS[1], 'backend_domain_fingerprint')
    or not redis.call('HGET', KEYS[1], 'membership_domain')
    or not redis.call('HGET', KEYS[1], 'membership_domain_fingerprint')
    or not redis.call('HGET', KEYS[1], 'active_backend_domain')
    or not redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint')) then
  local revision = tonumber(revision_raw)
  if revision_raw ~= '9007199254740991' then
    revision = redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  end
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_schema_incomplete',
    'updated_at_ms', tostring(now_ms()))
  return cjson.encode({
    status='ledger_corrupt', reason='ledger_schema_incomplete',
    ledger_revision=revision, ledger_incarnation=incarnation or '',
    committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0
  })
end
if revision_raw == '9007199254740991' then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_exhausted')
  return cjson.encode({
    status='ledger_corrupt', reason='ledger_revision_exhausted',
    ledger_revision=9007199254740991,
    ledger_incarnation=incarnation or ''
  })
end
local revision = redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
redis.call('HSET', KEYS[1],
  'resource_id', ARGV[1],
  'allocatable_mb', ARGV[2],
  'bootstrap_state', 'not_ready',
  'reconcile_deadline_ms', '0',
  'not_ready_reason', ARGV[3],
  'ledger_version', '3',
  'updated_at_ms', tostring(now_ms()))
redis.call('HSETNX', KEYS[1], 'committed_mb', '0')
redis.call('HSETNX', KEYS[1], 'backend_domain', '[]')
redis.call('HSETNX', KEYS[1], 'backend_domain_fingerprint', ARGV[4])
redis.call('HSETNX', KEYS[1], 'membership_domain', '[]')
redis.call('HSETNX', KEYS[1], 'membership_domain_fingerprint', ARGV[4])
redis.call('HSETNX', KEYS[1], 'active_backend_domain', '[]')
redis.call('HSETNX', KEYS[1], 'active_backend_domain_fingerprint', ARGV[4])
redis.call('HSETNX', KEYS[1], 'allocation_count', '0')
redis.call('HSETNX', KEYS[1], 'lease_counts', '{}')
redis.call('HSETNX', KEYS[1], 'card_queue_count', '0')
redis.call('HSETNX', KEYS[1], 'backend_queue_counts', '{}')
redis.call('HSETNX', KEYS[1], 'transition_mirror', '')
redis.call('HSETNX', KEYS[1], 'ledger_incarnation', ARGV[5])
return cjson.encode({
  status='not_ready', reason=ARGV[3], ledger_revision=revision,
  ledger_incarnation=redis.call('HGET', KEYS[1], 'ledger_incarnation'),
  committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0
})
"""
_BEGIN_PROOF_RESET_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function valid_positive_decimal(value, maximum)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < string.len(maximum) then return true end
  if string.len(value) > string.len(maximum) then return false end
  return value <= maximum
end
local function valid_revision(value)
  return valid_positive_decimal(value, '9007199254740991')
end
local function valid_epoch(value)
  return valid_positive_decimal(value, '9223372036854775807')
end
local function valid_fingerprint(value)
  return type(value) == 'string' and string.len(value) == 64
    and string.match(value, '^[0-9a-f]+$') ~= nil
end
local function card_type(key)
  local reply = redis.call('TYPE', key)
  if type(reply) == 'table' then return reply.ok end
  return reply
end
local function response(
    status, reason, revision, incarnation, committed, ready, idempotent)
  return cjson.encode({
    status=status, reason=reason or '',
    ledger_revision=revision or 0,
    ledger_incarnation=incarnation or '',
    committed_mb=committed or 0,
    purged_leases=0, ready=ready or false,
    idempotent=idempotent or false
  })
end
local function validate_domains(domain_raw, membership_raw, active_raw)
  local domain = decode(domain_raw)
  local memberships = decode(membership_raw)
  local active = decode(active_raw)
  if not domain or not memberships or not active
     or #domain > 64 or #memberships ~= #domain or #active > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#memberships == 0 and membership_raw ~= '[]')
     or (#active == 0 and active_raw ~= '[]') then
    return nil
  end
  local known = {}
  local valid_states = {pending=true, active=true, retiring=true}
  local previous = nil
  local active_index = 1
  for index, backend_id in ipairs(domain) do
    local member = memberships[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or known[backend_id]
       or type(member) ~= 'table' or table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not valid_epoch(member.membership_epoch)
       or not valid_states[member.state] then
      return nil
    end
    known[backend_id] = true
    previous = backend_id
    if member.state == 'active' then
      if active[active_index] ~= backend_id then return nil end
      active_index = active_index + 1
    end
  end
  if active_index ~= #active + 1 then return nil end
  return {domain=domain, known=known, active=active}
end
local function valid_stored_domain(raw)
  local domain = decode(raw)
  if not domain or #domain > 64
     or (#domain == 0 and raw ~= '[]') then
    return nil
  end
  local known = {}
  local previous = nil
  for _, backend_id in ipairs(domain) do
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or known[backend_id] then
      return nil
    end
    known[backend_id] = true
    previous = backend_id
  end
  return domain
end

if not valid_positive_decimal(ARGV[2], '9007199254740991') then
  return response('ledger_corrupt', 'allocatable_mb_invalid')
end
if (ARGV[3] == '') ~= (ARGV[4] == '') then
  return response('ledger_corrupt', 'proof_reset_cas_incomplete')
end
if ARGV[3] ~= '' and not valid_revision(ARGV[3]) then
  return response('ledger_corrupt', 'expected_ledger_revision_invalid')
end
if type(ARGV[4]) ~= 'string' or string.len(ARGV[4]) > 128 then
  return response('ledger_corrupt', 'expected_ledger_incarnation_invalid')
end
if type(ARGV[5]) ~= 'string' or ARGV[5] == ''
   or string.len(ARGV[5]) > 256 then
  return response('ledger_corrupt', 'proof_reset_id_invalid')
end
if not valid_fingerprint(ARGV[6]) then
  return response('ledger_corrupt', 'proof_reset_begin_fingerprint_invalid')
end
if type(ARGV[7]) ~= 'string' or ARGV[7] == ''
   or string.len(ARGV[7]) > 128 then
  return response('ledger_corrupt', 'proposed_incarnation_invalid')
end
for index = 9, 13, 2 do
  if not valid_fingerprint(ARGV[index]) then
    return response('ledger_corrupt', 'membership_domain_fingerprint_invalid')
  end
end
local target = validate_domains(ARGV[8], ARGV[10], ARGV[12])
if not target then
  return response('ledger_corrupt', 'target_membership_domain_invalid')
end
if #ARGV ~= 13 + #target.domain or #KEYS ~= 4 + (#target.domain * 2) then
  return response('ledger_corrupt', 'backend_key_domain_invalid')
end
for index, backend_id in ipairs(target.domain) do
  if ARGV[13 + index] ~= backend_id then
    return response('ledger_corrupt', 'backend_key_domain_invalid')
  end
end

local type_name = card_type(KEYS[1])
if type_name ~= 'none' and type_name ~= 'hash' then
  return response('ledger_corrupt', 'card_type_invalid')
end
local card_exists = type_name == 'hash'
local current_resource = card_exists
  and redis.call('HGET', KEYS[1], 'resource_id') or false
if card_exists and not current_resource then
  return response('ledger_corrupt', 'resource_identity_missing')
end
if card_exists and current_resource ~= ARGV[1] then
  return response('ledger_corrupt', 'resource_identity_mismatch')
end

if card_exists then
  local proof_state = redis.call('HGET', KEYS[1], 'proof_reset_state')
  if proof_state == 'prepared' then
    local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
    local current_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
    local prepared_version = redis.call('HGET', KEYS[1], 'ledger_version')
    if redis.call('HGET', KEYS[1], 'proof_reset_id') ~= ARGV[5] then
      return response('not_ready', 'proof_reset_in_progress',
        tonumber(current_revision or '0') or 0, current_incarnation, 0, false, false)
    end
    if redis.call('HGET', KEYS[1], 'proof_reset_begin_fingerprint') ~= ARGV[6] then
      return response('config_mismatch', 'proof_reset_id_conflict',
        tonumber(current_revision or '0') or 0, current_incarnation, 0, false, false)
    end
    if current_revision ~= redis.call('HGET', KEYS[1], 'proof_reset_revision')
       or current_incarnation ~= redis.call('HGET', KEYS[1], 'proof_reset_incarnation')
       or redis.call('HGET', KEYS[1], 'allocatable_mb') ~= ARGV[2]
       or redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[8]
       or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[9]
       or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[10]
       or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[11]
       or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[12]
       or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[13]
       or (prepared_version ~= '2' and prepared_version ~= '3')
       or redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'not_ready'
       or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= '0'
       or redis.call('HGET', KEYS[1], 'not_ready_reason') ~= 'proof_reset_in_progress'
       or not valid_positive_decimal(
         redis.call('HGET', KEYS[1], 'proof_reset_prepared_at_ms'),
         '9007199254740991')
       or not valid_revision(current_revision)
       or current_incarnation == '' or string.len(current_incarnation) > 128 then
      return response('ledger_corrupt', 'proof_reset_marker_invalid',
        tonumber(current_revision or '0') or 0, current_incarnation)
    end
    return response('prepared', 'proof_reset_in_progress',
      tonumber(current_revision), current_incarnation, 0, false, true)
  elseif proof_state then
    return response('ledger_corrupt', 'proof_reset_state_invalid')
  end

  if redis.call('HGET', KEYS[1], 'last_proof_reset_id') == ARGV[5] then
    local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
    local current_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
    if redis.call('HGET', KEYS[1], 'last_proof_reset_begin_fingerprint') ~= ARGV[6] then
      return response('config_mismatch', 'proof_reset_id_conflict',
        tonumber(current_revision or '0') or 0, current_incarnation)
    end
    return response('stale_revision', 'proof_reset_already_committed',
      tonumber(current_revision or '0') or 0, current_incarnation,
      tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0)
  end
end

local current_revision = card_exists
  and redis.call('HGET', KEYS[1], 'ledger_revision') or false
local current_incarnation = card_exists
  and redis.call('HGET', KEYS[1], 'ledger_incarnation') or false
local current_incarnation_valid = type(current_incarnation) == 'string'
  and current_incarnation ~= '' and string.len(current_incarnation) <= 128
local core_valid = valid_revision(current_revision) and current_incarnation_valid
if not card_exists then
  if ARGV[3] ~= '' then
    return response('stale_revision', 'card_missing', 0, '')
  end
elseif core_valid then
  if ARGV[3] == '' then
    return response('stale_revision', 'proof_reset_cas_required',
      tonumber(current_revision), current_incarnation,
      tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0)
  end
  if current_revision ~= ARGV[3] or current_incarnation ~= ARGV[4] then
    return response('stale_revision', 'ledger_changed',
      tonumber(current_revision), current_incarnation,
      tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0)
  end
elseif ARGV[3] ~= '' then
  return response('ledger_corrupt', 'corrupt_card_requires_no_cas',
    tonumber(current_revision or '0') or 0, current_incarnation or '')
end
if current_incarnation_valid and current_incarnation == ARGV[7] then
  return response('ledger_corrupt', 'proposed_incarnation_reused',
    tonumber(current_revision), current_incarnation)
end

if card_exists then
  local stored_domain = valid_stored_domain(
    redis.call('HGET', KEYS[1], 'backend_domain'))
  if stored_domain then
    for _, backend_id in ipairs(stored_domain) do
      if not target.known[backend_id] then
        return response('config_mismatch',
          'stored_backend_domain_exceeds_closed_domain',
          tonumber(current_revision or '0') or 0, current_incarnation or '')
      end
    end
  end
end

local preserved_fields = {
  'last_proof_reset_id', 'last_proof_reset_begin_fingerprint',
  'last_proof_reset_commit_fingerprint', 'last_proof_reset_revision',
  'last_proof_reset_incarnation', 'last_proof_reset_ready',
  'last_proof_reset_status', 'last_proof_reset_reason',
  'last_proof_fingerprint', 'last_proof_deadline_ms'
}
local preserved = {}
if card_exists then
  for _, field in ipairs(preserved_fields) do
    local value = redis.call('HGET', KEYS[1], field)
    if value then
      table.insert(preserved, field)
      table.insert(preserved, value)
    end
  end
end
local prepared_at = now_ms()
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1],
  'resource_id', ARGV[1],
  'allocatable_mb', ARGV[2],
  'bootstrap_state', 'not_ready',
  'reconcile_deadline_ms', '0',
  'not_ready_reason', 'proof_reset_in_progress',
  'ledger_version', '3',
  'ledger_revision', '1',
  'ledger_incarnation', ARGV[7],
  'backend_domain', ARGV[8],
  'backend_domain_fingerprint', ARGV[9],
  'membership_domain', ARGV[10],
  'membership_domain_fingerprint', ARGV[11],
  'active_backend_domain', ARGV[12],
  'active_backend_domain_fingerprint', ARGV[13],
  'committed_mb', '0',
  'allocation_count', '0',
  'lease_counts', '{}',
  'card_queue_count', '0',
  'backend_queue_counts', '{}',
  'transition_mirror', '',
  'proof_reset_state', 'prepared',
  'proof_reset_id', ARGV[5],
  'proof_reset_begin_fingerprint', ARGV[6],
  'proof_reset_revision', '1',
  'proof_reset_incarnation', ARGV[7],
  'proof_reset_prepared_at_ms', tostring(prepared_at),
  'updated_at_ms', tostring(prepared_at))
if #preserved > 0 then redis.call('HSET', KEYS[1], unpack(preserved)) end
return response('prepared', 'proof_reset_in_progress', 1, ARGV[7], 0, false, false)
"""
_COMMIT_PROOF_RESET_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local function valid_positive_decimal(value, maximum)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < string.len(maximum) then return true end
  if string.len(value) > string.len(maximum) then return false end
  return value <= maximum
end
local function valid_revision(value)
  return valid_positive_decimal(value, '9007199254740991')
end
local function valid_generation(value)
  return valid_positive_decimal(value, '9223372036854775807')
end
local function valid_fingerprint(value)
  return type(value) == 'string' and string.len(value) == 64
    and string.match(value, '^[0-9a-f]+$') ~= nil
end
local function card_type(key)
  local reply = redis.call('TYPE', key)
  if type(reply) == 'table' then return reply.ok end
  return reply
end
local function response(
    status, reason, revision, incarnation, committed, ready, idempotent)
  return cjson.encode({
    status=status, reason=reason or '',
    ledger_revision=revision or 0,
    ledger_incarnation=incarnation or '',
    committed_mb=committed or 0,
    purged_leases=0, ready=ready or false,
    idempotent=idempotent or false
  })
end
local function validate_domains(domain_raw, membership_raw, active_raw)
  local domain = decode(domain_raw)
  local memberships = decode(membership_raw)
  local active = decode(active_raw)
  if not domain or not memberships or not active
     or #domain > 64 or #memberships ~= #domain or #active > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#memberships == 0 and membership_raw ~= '[]')
     or (#active == 0 and active_raw ~= '[]') then
    return nil
  end
  local known = {}
  local valid_states = {pending=true, active=true, retiring=true}
  local previous = nil
  local active_index = 1
  for index, backend_id in ipairs(domain) do
    local member = memberships[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or known[backend_id]
       or type(member) ~= 'table' or table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not valid_generation(member.membership_epoch)
       or not valid_states[member.state] then
      return nil
    end
    known[backend_id] = true
    previous = backend_id
    if member.state == 'active' then
      if active[active_index] ~= backend_id then return nil end
      active_index = active_index + 1
    end
  end
  if active_index ~= #active + 1 then return nil end
  return {domain=domain, known=known, active=active}
end

local function valid_allocation(allocation, known)
  local generation_valid = false
  if type(allocation) == 'table' then
    generation_valid = valid_generation(allocation.generation)
      or (allocation.generation == cjson.null and allocation.state == 'unknown')
  end
  return type(allocation) == 'table' and table_size(allocation) == 9
    and type(allocation.backend_id) == 'string'
    and known[allocation.backend_id]
    and (allocation.state == 'resident' or allocation.state == 'unknown')
    and valid_integer(allocation.budget_mb, 1, 9007199254740991)
    and generation_valid
    and valid_integer(allocation.eviction_priority,
      -9007199254740991, 9007199254740991)
    and type(allocation.evictable) == 'boolean'
    and (allocation.state ~= 'unknown' or allocation.evictable == false)
    and valid_integer(allocation.max_concurrency, 1, 10000)
    and valid_integer(allocation.last_used_at_ms, 1, 9007199254740991)
    and valid_integer(allocation.not_evict_before_ms, 0, 9007199254740991)
    and (allocation.state ~= 'resident' or allocation.not_evict_before_ms > 0)
end
local function allocations_equal(left, right)
  return left and right
    and left.backend_id == right.backend_id
    and left.state == right.state
    and left.budget_mb == right.budget_mb
    and left.generation == right.generation
    and left.eviction_priority == right.eviction_priority
    and left.evictable == right.evictable
    and left.max_concurrency == right.max_concurrency
    and left.last_used_at_ms == right.last_used_at_ms
    and left.not_evict_before_ms == right.not_evict_before_ms
end
local function validate_allocations(raw, target)
  local allocations = decode(raw)
  if not allocations or #allocations > 64
     or (#allocations == 0 and raw ~= '[]') then
    return nil
  end
  local seen = {}
  local by_backend = {}
  local committed = 0
  local has_unknown = false
  for _, allocation in ipairs(allocations) do
    if not valid_allocation(allocation, target.known)
       or seen[allocation.backend_id] then
      return nil
    end
    seen[allocation.backend_id] = true
    by_backend[allocation.backend_id] = allocation
    if allocation.state == 'unknown' then has_unknown = true end
    committed = committed + allocation.budget_mb
    if committed > 9007199254740991 then return nil end
  end
  return {
    items=allocations, by_backend=by_backend,
    committed=committed, has_unknown=has_unknown
  }
end
local function valid_zero_map(raw, domain)
  local value = decode(raw)
  if not value or table_size(value) ~= #domain then return false end
  for _, backend_id in ipairs(domain) do
    if value[backend_id] ~= 0 then return false end
  end
  return true
end

if type(ARGV[3]) ~= 'string' or ARGV[3] == '' or string.len(ARGV[3]) > 256 then
  return response('ledger_corrupt', 'proof_reset_id_invalid')
end
if not valid_revision(ARGV[4]) then
  return response('ledger_corrupt', 'expected_reset_revision_invalid')
end
if type(ARGV[5]) ~= 'string' or ARGV[5] == '' or string.len(ARGV[5]) > 128 then
  return response('ledger_corrupt', 'expected_reset_incarnation_invalid')
end
if ARGV[6] ~= '0' and ARGV[6] ~= '1' then
  return response('ledger_corrupt', 'proof_ready_invalid')
end
if not valid_positive_decimal(ARGV[2], '9007199254740991') then
  return response('ledger_corrupt', 'allocatable_mb_invalid')
end
if (ARGV[6] == '1'
    and not valid_positive_decimal(ARGV[7], '9007199254740991'))
   or (ARGV[6] == '0' and ARGV[7] ~= '0') then
  return response('ledger_corrupt', 'proof_deadline_invalid')
end
if not valid_fingerprint(ARGV[8]) or not valid_fingerprint(ARGV[9]) then
  return response('ledger_corrupt', 'proof_fingerprint_invalid')
end
for index = 12, 16, 2 do
  if not valid_fingerprint(ARGV[index]) then
    return response('ledger_corrupt', 'membership_domain_fingerprint_invalid')
  end
end
local target = validate_domains(ARGV[11], ARGV[13], ARGV[15])
if not target then
  return response('ledger_corrupt', 'target_membership_domain_invalid')
end
if #ARGV ~= 16 + #target.domain or #KEYS ~= 4 + (#target.domain * 2) then
  return response('ledger_corrupt', 'backend_key_domain_invalid')
end
for index, backend_id in ipairs(target.domain) do
  if ARGV[16 + index] ~= backend_id then
    return response('ledger_corrupt', 'backend_key_domain_invalid')
  end
end
if ARGV[6] == '1' and #target.active == 0 then
  return response('not_ready', 'active_backend_domain_empty')
end
local proof_allocations = validate_allocations(ARGV[10], target)
if not proof_allocations then
  return response('ledger_corrupt', 'proof_allocation_invalid')
end
local final_ready = ARGV[6] == '1' and not proof_allocations.has_unknown
  and proof_allocations.committed <= tonumber(ARGV[2])
local final_status = final_ready and 'reconciled' or 'not_ready'
local final_reason = ''
if proof_allocations.committed > tonumber(ARGV[2]) then
  final_reason = 'committed_exceeds_allocatable'
elseif not final_ready then
  final_reason = 'proof_incomplete'
end
local function committed_state_valid(marker_revision, marker_incarnation)
  local expected_bootstrap_state = final_ready and 'ready' or 'not_ready'
  local expected_deadline = final_ready and ARGV[7] or '0'
  if marker_revision ~= '2'
     or redis.call('TTL', KEYS[1]) ~= -1
     or redis.call('HGET', KEYS[1], 'ledger_version') ~= '3'
     or redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[1]
     or redis.call('HGET', KEYS[1], 'allocatable_mb') ~= ARGV[2]
     or redis.call('HGET', KEYS[1], 'ledger_revision') ~= marker_revision
     or redis.call('HGET', KEYS[1], 'ledger_incarnation') ~= marker_incarnation
     or redis.call('HGET', KEYS[1], 'bootstrap_state') ~= expected_bootstrap_state
     or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= expected_deadline
     or redis.call('HGET', KEYS[1], 'not_ready_reason') ~= final_reason
     or redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[11]
     or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[12]
     or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[13]
     or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[14]
     or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[15]
     or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[16]
     or redis.call('HGET', KEYS[1], 'committed_mb')
       ~= tostring(proof_allocations.committed)
     or redis.call('HGET', KEYS[1], 'allocation_count')
       ~= tostring(#proof_allocations.items)
     or not valid_zero_map(
       redis.call('HGET', KEYS[1], 'lease_counts'), target.domain)
     or redis.call('HGET', KEYS[1], 'card_queue_count') ~= '0'
     or not valid_zero_map(
       redis.call('HGET', KEYS[1], 'backend_queue_counts'), target.domain)
     or redis.call('HGET', KEYS[1], 'transition_mirror') ~= ''
     or redis.call('HGET', KEYS[1], 'proof_reset_state')
     or redis.call('HGET', KEYS[1], 'last_proof_reset_id') ~= ARGV[3]
     or not valid_fingerprint(redis.call(
       'HGET', KEYS[1], 'last_proof_reset_begin_fingerprint'))
     or redis.call('HGET', KEYS[1], 'last_proof_reset_commit_fingerprint') ~= ARGV[9]
     or redis.call('HGET', KEYS[1], 'last_proof_reset_revision') ~= marker_revision
     or redis.call('HGET', KEYS[1], 'last_proof_reset_incarnation') ~= marker_incarnation
     or redis.call('HGET', KEYS[1], 'last_proof_reset_ready')
       ~= (final_ready and '1' or '0')
     or redis.call('HGET', KEYS[1], 'last_proof_reset_status') ~= final_status
     or redis.call('HGET', KEYS[1], 'last_proof_reset_reason') ~= final_reason
     or redis.call('HGET', KEYS[1], 'last_proof_fingerprint') ~= ARGV[8]
     or redis.call('HGET', KEYS[1], 'last_proof_deadline_ms') ~= ARGV[7]
     or not valid_positive_decimal(
       redis.call('HGET', KEYS[1], 'updated_at_ms'), '9007199254740991') then
    return false
  end
  local allocation_type = card_type(KEYS[2])
  if #proof_allocations.items == 0 then
    if allocation_type ~= 'none' then return false end
  else
    if allocation_type ~= 'hash' or redis.call('TTL', KEYS[2]) ~= -1
       or redis.call('HLEN', KEYS[2]) ~= #proof_allocations.items then
      return false
    end
    for backend_id, expected in pairs(proof_allocations.by_backend) do
      local actual = decode(redis.call('HGET', KEYS[2], backend_id))
      if not valid_allocation(actual, target.known)
         or not allocations_equal(actual, expected) then
        return false
      end
    end
  end
  if card_type(KEYS[3]) ~= 'none' or card_type(KEYS[4]) ~= 'none' then
    return false
  end
  for index = 5, #KEYS do
    if card_type(KEYS[index]) ~= 'none' then return false end
  end
  return true
end

local type_name = card_type(KEYS[1])
if type_name ~= 'hash' then
  return response(type_name == 'none' and 'not_ready' or 'ledger_corrupt',
    type_name == 'none' and 'card_missing' or 'card_type_invalid')
end
if redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[1] then
  return response('ledger_corrupt', 'resource_identity_mismatch')
end

if redis.call('HGET', KEYS[1], 'last_proof_reset_id') == ARGV[3] then
  local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
  local current_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
  if redis.call('HGET', KEYS[1], 'last_proof_reset_commit_fingerprint') ~= ARGV[9] then
    return response('config_mismatch', 'proof_reset_id_conflict',
      tonumber(current_revision or '0') or 0, current_incarnation)
  end
  local marker_revision = redis.call('HGET', KEYS[1], 'last_proof_reset_revision')
  local marker_incarnation = redis.call('HGET', KEYS[1], 'last_proof_reset_incarnation')
  if not valid_revision(marker_revision) or not marker_incarnation
     or marker_incarnation == '' or string.len(marker_incarnation) > 128 then
    return response('ledger_corrupt', 'last_proof_reset_marker_invalid',
      tonumber(current_revision or '0') or 0, current_incarnation)
  end
  if current_revision ~= marker_revision or current_incarnation ~= marker_incarnation then
    return response('stale_revision', 'ledger_changed_after_proof_reset',
      tonumber(current_revision or '0') or 0, current_incarnation,
      tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0)
  end
  local last_status = redis.call('HGET', KEYS[1], 'last_proof_reset_status')
  local last_ready = redis.call('HGET', KEYS[1], 'last_proof_reset_ready')
  if (last_status ~= 'reconciled' and last_status ~= 'not_ready')
     or (last_ready ~= '0' and last_ready ~= '1') then
    return response('ledger_corrupt', 'last_proof_reset_marker_invalid',
      tonumber(current_revision), current_incarnation)
  end
  if not committed_state_valid(marker_revision, marker_incarnation) then
    return response('ledger_corrupt', 'proof_reset_committed_state_invalid',
      tonumber(current_revision), current_incarnation,
      proof_allocations.committed, false, true)
  end
  if last_ready == '1' then
    local retry_now = now_ms()
    local retry_deadline = tonumber(ARGV[7])
    if retry_deadline <= retry_now then
      return response('not_ready', 'reconcile_expired',
        tonumber(current_revision), current_incarnation,
        proof_allocations.committed, false, true)
    end
    if retry_deadline > retry_now + 300000 then
      return response('not_ready', 'reconcile_evidence_invalid',
        tonumber(current_revision), current_incarnation,
        proof_allocations.committed, false, true)
    end
  end
  return response(last_status,
    redis.call('HGET', KEYS[1], 'last_proof_reset_reason') or '',
    tonumber(current_revision), current_incarnation,
    proof_allocations.committed,
    last_ready == '1', true)
end

local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
local current_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
if redis.call('HGET', KEYS[1], 'proof_reset_state') ~= 'prepared'
   or redis.call('HGET', KEYS[1], 'proof_reset_id') ~= ARGV[3] then
  return response('not_ready', 'proof_reset_not_prepared',
    tonumber(current_revision or '0') or 0, current_incarnation)
end
if current_revision ~= ARGV[4] or current_incarnation ~= ARGV[5]
   or redis.call('HGET', KEYS[1], 'proof_reset_revision') ~= ARGV[4]
   or redis.call('HGET', KEYS[1], 'proof_reset_incarnation') ~= ARGV[5] then
  return response('stale_revision', 'proof_reset_context_changed',
    tonumber(current_revision or '0') or 0, current_incarnation)
end
local prepared_at = redis.call('HGET', KEYS[1], 'proof_reset_prepared_at_ms')
local prepared_version = redis.call('HGET', KEYS[1], 'ledger_version')
if (prepared_version ~= '2' and prepared_version ~= '3')
   or redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'not_ready'
   or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= '0'
   or redis.call('HGET', KEYS[1], 'not_ready_reason') ~= 'proof_reset_in_progress'
   or not valid_positive_decimal(prepared_at, '9007199254740991') then
  return response('ledger_corrupt', 'proof_reset_marker_invalid',
    tonumber(current_revision), current_incarnation)
end
local begin_fingerprint = redis.call(
  'HGET', KEYS[1], 'proof_reset_begin_fingerprint')
if not valid_fingerprint(begin_fingerprint) then
  return response('ledger_corrupt', 'proof_reset_marker_invalid',
    tonumber(current_revision), current_incarnation)
end
if redis.call('HGET', KEYS[1], 'allocatable_mb') ~= ARGV[2]
   or redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[11]
   or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[12]
   or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[13]
   or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[14]
   or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[15]
   or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[16] then
  return response('config_mismatch', 'proof_reset_domain_changed',
    tonumber(current_revision), current_incarnation)
end

local now = now_ms()
local deadline = tonumber(ARGV[7])
if ARGV[6] == '1'
   and (not valid_integer(deadline, 1, 9007199254740991)
        or deadline <= now or deadline > now + 300000) then
  return response('not_ready', 'proof_evidence_expired',
    tonumber(current_revision), current_incarnation)
end
local lease_counts = {}
local backend_queue_counts = {}
for _, backend_id in ipairs(target.domain) do
  lease_counts[backend_id] = 0
  backend_queue_counts[backend_id] = 0
end

for index = 2, #KEYS do redis.call('DEL', KEYS[index]) end
redis.call('DEL', KEYS[1])
for _, allocation in ipairs(proof_allocations.items) do
  redis.call('HSET', KEYS[2], allocation.backend_id, cjson.encode(allocation))
end
redis.call('HSET', KEYS[1],
  'resource_id', ARGV[1],
  'allocatable_mb', ARGV[2],
  'bootstrap_state', final_ready and 'ready' or 'not_ready',
  'reconcile_deadline_ms', final_ready and ARGV[7] or '0',
  'not_ready_reason', final_reason,
  'ledger_version', '3',
  'ledger_revision', '2',
  'ledger_incarnation', ARGV[5],
  'backend_domain', ARGV[11],
  'backend_domain_fingerprint', ARGV[12],
  'membership_domain', ARGV[13],
  'membership_domain_fingerprint', ARGV[14],
  'active_backend_domain', ARGV[15],
  'active_backend_domain_fingerprint', ARGV[16],
  'committed_mb', tostring(proof_allocations.committed),
  'allocation_count', tostring(#proof_allocations.items),
  'lease_counts', cjson.encode(lease_counts),
  'card_queue_count', '0',
  'backend_queue_counts', cjson.encode(backend_queue_counts),
  'transition_mirror', '',
  'last_proof_reset_id', ARGV[3],
  'last_proof_reset_begin_fingerprint', begin_fingerprint,
  'last_proof_reset_commit_fingerprint', ARGV[9],
  'last_proof_reset_revision', '2',
  'last_proof_reset_incarnation', ARGV[5],
  'last_proof_reset_ready', final_ready and '1' or '0',
  'last_proof_reset_status', final_status,
  'last_proof_reset_reason', final_reason,
  'last_proof_fingerprint', ARGV[8],
  'last_proof_deadline_ms', ARGV[7],
  'updated_at_ms', tostring(now))
return response(final_status, final_reason, 2, ARGV[5],
  proof_allocations.committed,
  final_ready, false)
"""
_RECONCILE_CARD_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function generation_less(left, right)
  if string.len(left) ~= string.len(right) then
    return string.len(left) < string.len(right)
  end
  return left < right
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_generation(item.generation)
end
local function allocation_generation_regressed(target, current)
  if current.generation == cjson.null then return false end
  if target.generation == cjson.null then return true end
  return generation_less(target.generation, current.generation)
end
local function valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local valid_states = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
local counted_states = {
  unknown=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true
}
local valid_lease_states = {active=true, uncertain=true, stale=true}
local function valid_lease(lease, lease_id, backend_id)
  return lease and lease.lease_id == lease_id and lease.backend_id == backend_id
    and type(lease.owner_id) == 'string' and lease.owner_id ~= ''
    and type(lease.operation) == 'string' and lease.operation ~= ''
    and valid_generation(lease.generation) and valid_lease_states[lease.state]
    and valid_integer(lease.created_at_ms, 1, 9007199254740991)
    and valid_integer(lease.heartbeat_deadline_ms, 1, 9007199254740991)
    and valid_integer(lease.hard_deadline_ms, 1, 9007199254740991)
    and lease.created_at_ms <= lease.heartbeat_deadline_ms
    and lease.heartbeat_deadline_ms <= lease.hard_deadline_ms
end
local function valid_allocation(item, backend_id)
  if type(item) ~= 'table' then return false end
  local reservation_state = item
    and (item.state == 'reserving' or item.state == 'loading')
  local has_reservation = item
    and type(item.reservation_lease_id) == 'string'
    and item.reservation_lease_id ~= ''
    and type(item.reservation_owner_id) == 'string'
    and item.reservation_owner_id ~= ''
  local reservation_present = item
    and (item.reservation_lease_id ~= nil
      or item.reservation_owner_id ~= nil)
  return item and item.backend_id == backend_id and valid_states[item.state]
    and valid_integer(item.budget_mb, 1, 9007199254740991)
    and valid_allocation_generation(item)
    and valid_integer(item.eviction_priority, -9007199254740991, 9007199254740991)
    and type(item.evictable) == 'boolean'
    and valid_integer(item.max_concurrency, 1, 10000)
    and valid_integer(item.last_used_at_ms, 1, 9007199254740991)
    and valid_integer(item.not_evict_before_ms, 0, 9007199254740991)
    and (item.state ~= 'resident' or item.not_evict_before_ms > 0)
    and (not reservation_state or has_reservation)
    and (reservation_state or not reservation_present)
end
local function read_queue(
    key, now, expected_kind, expected_backend_id, known_backends,
    expected_raw_count)
  local raw_count = redis.call('LLEN', key)
  if raw_count ~= expected_raw_count or raw_count > 10000 then
    return nil, raw_count
  end
  local entries = redis.call('LRANGE', key, 0, -1)
  local seen = {}
  local live_count = 0
  for _, raw in ipairs(entries) do
    local ticket = decode(raw)
    if not ticket
       or not valid_integer(ticket.expires_at_ms, 1, 9007199254740991)
       or not valid_integer(ticket.enqueued_at_ms, 1, 9007199254740991)
       or ticket.enqueued_at_ms > ticket.expires_at_ms
       or type(ticket.ticket_id) ~= 'string' or ticket.ticket_id == ''
       or type(ticket.owner_id) ~= 'string' or ticket.owner_id == ''
       or type(ticket.backend_id) ~= 'string' or ticket.backend_id == ''
       or not valid_generation(ticket.membership_epoch)
       or ticket.kind ~= expected_kind
       or not known_backends[ticket.backend_id]
       or (expected_backend_id ~= ''
         and ticket.backend_id ~= expected_backend_id)
       or seen[ticket.ticket_id] then
      return nil, raw_count
    end
    seen[ticket.ticket_id] = true
    if ticket.expires_at_ms > now then live_count = live_count + 1 end
  end
  return live_count, raw_count
end

local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end

local function allocations_equal(left, right)
  return left and right
    and left.backend_id == right.backend_id
    and left.state == right.state
    and left.budget_mb == right.budget_mb
    and left.generation == right.generation
    and left.eviction_priority == right.eviction_priority
    and left.evictable == right.evictable
    and left.max_concurrency == right.max_concurrency
    and left.reservation_lease_id == right.reservation_lease_id
    and left.reservation_owner_id == right.reservation_owner_id
    and left.last_used_at_ms == right.last_used_at_ms
    and left.not_evict_before_ms == right.not_evict_before_ms
end

local now = now_ms()
local requested_deadline = tonumber(ARGV[5])
local supplied_expected_revision = ARGV[2]
local supplied_expected_incarnation = ARGV[16]
local proposed_incarnation = ARGV[17]
local expected_revision = supplied_expected_revision
local expected_incarnation = supplied_expected_incarnation
local existing_resource = redis.call('HGET', KEYS[1], 'resource_id')
local current_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
local idempotent_retry = false
if existing_resource == ARGV[1]
   and redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
  return cjson.encode({
    status='not_ready', ready=false,
    ledger_revision=tonumber(
      redis.call('HGET', KEYS[1], 'ledger_revision') or '0') or 0,
    ledger_incarnation=current_incarnation,
    committed_mb=0, purged_leases=0,
    reason='proof_reset_in_progress'
  })
end
if type(proposed_incarnation) ~= 'string' or proposed_incarnation == ''
   or string.len(proposed_incarnation) > 128 then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='proposed_incarnation_invalid'})
end
if (supplied_expected_revision == '') ~= (supplied_expected_incarnation == '') then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='repair_cas_incomplete'})
end
if existing_resource == ARGV[1]
   and redis.call('HGET', KEYS[1], 'last_repair_id') == ARGV[6]
   and redis.call('HGET', KEYS[1], 'last_repair_fingerprint') == ARGV[9]
   and redis.call('HGET', KEYS[1], 'last_repair_expected_revision') == supplied_expected_revision
   and redis.call('HGET', KEYS[1], 'last_repair_expected_incarnation') == supplied_expected_incarnation then
  local marker_revision = redis.call('HGET', KEYS[1], 'last_repair_revision')
  local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
  if not marker_revision or not string.match(marker_revision, '^[1-9][0-9]*$')
     or string.len(marker_revision) > 16
     or (string.len(marker_revision) == 16
       and marker_revision > '9007199254740991') then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='last_repair_revision_invalid'})
  end
  if current_revision ~= marker_revision then
    return cjson.encode({status='stale_revision', ready=false, ledger_revision=tonumber(current_revision or '0') or 0, ledger_incarnation=current_incarnation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0, purged_leases=0, reason='ledger_changed_after_repair'})
  end
  if current_incarnation == '' or string.len(current_incarnation) > 128 then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(current_revision or '0') or 0, ledger_incarnation='', committed_mb=0, purged_leases=0, reason='ledger_incarnation_invalid'})
  end
  expected_revision = marker_revision
  expected_incarnation = current_incarnation
  idempotent_retry = true
end
if not idempotent_retry and expected_revision == '' then
  for i = 1, #KEYS do
    if redis.call('EXISTS', KEYS[i]) ~= 0 then
      return cjson.encode({status='partial_state', ready=false, ledger_revision=0, ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='bootstrap_keys_present'})
    end
  end
  current_incarnation = proposed_incarnation
end
if not idempotent_retry
   and (not valid_integer(requested_deadline, 1, 9007199254740991)
     or requested_deadline <= now or requested_deadline > now + 300000) then
  return cjson.encode({status='not_ready', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='reconcile_evidence_expired'})
end
if expected_revision ~= '' then
  if not existing_resource or existing_resource ~= ARGV[1] then
    return cjson.encode({status='not_ready', ready=false, ledger_revision=0, ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='card_missing_or_mismatched'})
  end
  local revision_raw = redis.call('HGET', KEYS[1], 'ledger_revision')
  if not revision_raw or not string.match(revision_raw, '^[1-9][0-9]*$')
     or string.len(revision_raw) > 16
     or (string.len(revision_raw) == 16
       and revision_raw > '9007199254740991') then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='ledger_revision_invalid'})
  end
  if revision_raw ~= expected_revision then
    return cjson.encode({status='stale_revision', ready=false, ledger_revision=tonumber(revision_raw), ledger_incarnation=current_incarnation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0'), purged_leases=0, reason='ledger_changed'})
  end
  if current_incarnation == '' or string.len(current_incarnation) > 128 then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(revision_raw), ledger_incarnation='', committed_mb=0, purged_leases=0, reason='ledger_incarnation_invalid'})
  end
  if current_incarnation ~= expected_incarnation then
    return cjson.encode({status='stale_revision', ready=false, ledger_revision=tonumber(revision_raw), ledger_incarnation=current_incarnation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0'), purged_leases=0, reason='ledger_incarnation_changed'})
  end
  local card_allocatable = tonumber(redis.call('HGET', KEYS[1], 'allocatable_mb') or '-1')
  local card_committed = tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1')
  local card_deadline = tonumber(redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '-1')
  local card_updated_at = tonumber(redis.call('HGET', KEYS[1], 'updated_at_ms') or '-1')
  local allocation_count = tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1')
  local card_queue_count = tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1')
  local bootstrap_state = redis.call('HGET', KEYS[1], 'bootstrap_state')
  if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3'
     or not valid_integer(card_allocatable, 1, 9007199254740991)
     or not valid_integer(card_committed, 0, 9007199254740991)
     or not valid_integer(card_deadline, 0, 9007199254740991)
     or not valid_integer(card_updated_at, 1, 9007199254740991)
     or not valid_integer(allocation_count, 0, 9007199254740991)
     or not valid_integer(card_queue_count, 0, 9007199254740991)
     or (bootstrap_state ~= 'ready' and bootstrap_state ~= 'not_ready') then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(revision_raw), committed_mb=0, purged_leases=0, reason='card_schema_invalid'})
  end
end

local lease_keys = {}
local queue_keys = {}
local known_backends = {}
local initialize_domain = false
local key_index = 5
for i = 18, #ARGV do
  local backend_id = ARGV[i]
  if type(backend_id) ~= 'string' or backend_id == '' or known_backends[backend_id] then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='backend_domain_invalid'})
  end
  known_backends[backend_id] = true
  lease_keys[backend_id] = KEYS[key_index]
  queue_keys[backend_id] = KEYS[key_index + 1]
  key_index = key_index + 2
end
if key_index - 1 ~= #KEYS then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='backend_key_domain_invalid'})
end
local requested_domain = decode(ARGV[10])
if not requested_domain or #requested_domain > 64
   or #requested_domain ~= table_size(known_backends) then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='backend_domain_invalid'})
end
for index, backend_id in ipairs(requested_domain) do
  if ARGV[index + 17] ~= backend_id then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='backend_domain_invalid'})
  end
end
local requested_membership_domain = decode(ARGV[12])
local valid_membership_states = {pending=true, active=true, retiring=true}
if not requested_membership_domain
   or #requested_membership_domain ~= #requested_domain
   or ARGV[12] == '' then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='membership_domain_invalid'})
end
local membership_by_backend = {}
for index, member in ipairs(requested_membership_domain) do
  local backend_id = requested_domain[index]
  if type(member) ~= 'table' or table_size(member) ~= 3
     or member.backend_id ~= backend_id
     or type(member.membership_epoch) ~= 'string'
     or not valid_generation(member.membership_epoch)
     or not valid_membership_states[member.state] then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='membership_domain_invalid'})
  end
  membership_by_backend[backend_id] = member
end
local requested_active_domain = decode(ARGV[14])
local active_backends = {}
local previous_active_backend = nil
if not requested_active_domain or #requested_active_domain > 64
   or ARGV[14] == '' then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='active_backend_domain_invalid'})
end
for _, backend_id in ipairs(requested_active_domain) do
  if type(backend_id) ~= 'string' or backend_id == ''
     or not known_backends[backend_id] or active_backends[backend_id]
     or (previous_active_backend and backend_id <= previous_active_backend) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='active_backend_domain_invalid'})
  end
  active_backends[backend_id] = true
  previous_active_backend = backend_id
end
if table_size(active_backends) ~= #requested_active_domain then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='active_backend_domain_invalid'})
end
for _, member in ipairs(requested_membership_domain) do
  if (member.state == 'active') ~= (active_backends[member.backend_id] == true) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='active_membership_domain_mismatch'})
  end
end
if ARGV[4] == '1' and #requested_active_domain == 0 then
  return cjson.encode({status='not_ready', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='active_backend_domain_empty'})
end
if expected_revision ~= '' then
  local stored_domain = redis.call('HGET', KEYS[1], 'backend_domain')
  local stored_fingerprint = redis.call('HGET', KEYS[1], 'backend_domain_fingerprint')
  local stored_membership_domain = redis.call('HGET', KEYS[1], 'membership_domain')
  local stored_membership_fingerprint = redis.call('HGET', KEYS[1], 'membership_domain_fingerprint')
  local stored_active_domain = redis.call('HGET', KEYS[1], 'active_backend_domain')
  local stored_active_fingerprint = redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint')
  initialize_domain = not idempotent_retry and ARGV[10] ~= '[]'
    and stored_domain == '[]'
    and stored_membership_domain == '[]'
    and stored_active_domain == '[]'
    and redis.call('HGET', KEYS[1], 'bootstrap_state') == 'not_ready'
    and redis.call('HGET', KEYS[1], 'allocation_count') == '0'
    and redis.call('HGET', KEYS[1], 'committed_mb') == '0'
  if not initialize_domain
     and (stored_domain ~= ARGV[10] or stored_fingerprint ~= ARGV[11]
       or stored_membership_domain ~= ARGV[12]
       or stored_membership_fingerprint ~= ARGV[13]
       or stored_active_domain ~= ARGV[14]
       or stored_active_fingerprint ~= ARGV[15]) then
    return cjson.encode({status='config_mismatch', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=0, purged_leases=0, reason='backend_domain_changed'})
  end
  if initialize_domain then current_incarnation = proposed_incarnation end
end

local target_allocations = decode(ARGV[7])
local cleanup_evidence = decode(ARGV[8])
if not target_allocations or not cleanup_evidence then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='repair_payload_invalid'})
end
local target_by_backend = {}
local target_order = {}
for _, allocation in ipairs(target_allocations) do
  if type(allocation) ~= 'table'
     or not valid_allocation(allocation, allocation.backend_id)
     or not known_backends[allocation.backend_id]
     or target_by_backend[allocation.backend_id] then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='target_allocation_invalid'})
  end
  target_by_backend[allocation.backend_id] = allocation
  table.insert(target_order, allocation.backend_id)
end

local current_by_backend = {}
local current_count = 0
local current_committed = 0
if redis.call('HLEN', KEYS[2]) > 64 then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=0, purged_leases=0, reason='allocation_domain_exceeded'})
end
local allocation_entries = redis.call('HGETALL', KEYS[2])
for i = 1, #allocation_entries, 2 do
  local backend_id = allocation_entries[i]
  local allocation = decode(allocation_entries[i + 1])
  if not known_backends[backend_id]
     or not valid_allocation(allocation, backend_id) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=0, purged_leases=0, reason='current_allocation_invalid'})
  end
  current_by_backend[backend_id] = allocation
  current_count = current_count + 1
  if counted_states[allocation.state] then
    current_committed = current_committed + allocation.budget_mb
  end
end
if expected_revision ~= '' then
  local cached_committed = tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1')
  local cached_count = tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1')
  if cached_committed ~= current_committed or cached_count ~= current_count then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='committed_cache_drift'})
  end
end

local leases_by_backend = {}
local lease_counts = {}
local cached_lease_counts = decode(redis.call('HGET', KEYS[1], 'lease_counts'))
if expected_revision ~= '' and not initialize_domain
   and (not cached_lease_counts or table_size(cached_lease_counts) ~= table_size(known_backends)) then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='lease_count_cache_invalid'})
end
for backend_id, lease_key in pairs(lease_keys) do
  local leases = {}
  local count = 0
  if redis.call('HLEN', lease_key) > 10000 then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='lease_domain_exceeded'})
  end
  local entries = redis.call('HGETALL', lease_key)
  for i = 1, #entries, 2 do
    local lease = decode(entries[i + 1])
    if not valid_lease(lease, entries[i], backend_id) then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='lease_invalid'})
    end
    leases[entries[i]] = lease
    count = count + 1
  end
  if count > 0 and not current_by_backend[backend_id] then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='orphan_leases'})
  end
  if current_by_backend[backend_id]
     and count > current_by_backend[backend_id].max_concurrency then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='lease_domain_exceeded'})
  end
  leases_by_backend[backend_id] = leases
  lease_counts[backend_id] = count
  if expected_revision ~= '' and not initialize_domain
     and cached_lease_counts[backend_id] ~= count then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='lease_count_cache_drift'})
  end
end

local transition_raw = redis.call('GET', KEYS[4])
local transition_mirror = ''
if expected_revision ~= '' then
  transition_mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
end
local transition_now = now_ms()
if expected_revision ~= '' and transition_mirror == false then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='transition_mirror_missing'})
end
if transition_raw then
  local transition = decode(transition_raw)
  if transition_raw ~= transition_mirror
     or not transition or transition.resource_id ~= ARGV[1]
     or not known_backends[transition.backend_id]
     or type(transition.owner_id) ~= 'string' or transition.owner_id == ''
     or not valid_generation(transition.generation)
     or type(transition.operation) ~= 'string' or transition.operation == ''
     or type(transition.require_idle) ~= 'boolean'
     or (transition.eviction_branch ~= nil
       and transition.eviction_branch ~= 'cancel'
       and transition.eviction_branch ~= 'unload')
     or not valid_integer(transition.created_at_ms, 1, 9007199254740991)
     or not valid_integer(transition.expires_at_ms, 1, 9007199254740991)
     or (transition.hard_deadline_ms ~= nil
       and (not valid_integer(
         transition.hard_deadline_ms, 1, 9007199254740991)
         or transition.expires_at_ms > transition.hard_deadline_ms)) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='transition_invalid'})
  end
  if transition.eviction_branch ~= nil
     or transition.expires_at_ms > transition_now then
    local current_deadline = tonumber(
      redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '0')
    local current_ready = redis.call('HGET', KEYS[1], 'bootstrap_state') == 'ready'
      and tonumber(expected_revision ~= '' and expected_revision or '0')
        < 9007199252740991
      and valid_integer(current_deadline, 1, 9007199254740991)
      and current_deadline > transition_now
      and current_deadline <= transition_now + 300000
    return cjson.encode({status='busy', ready=current_ready, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='transition_active'})
  end
elseif transition_mirror ~= '' then
  local mirrored_transition = decode(transition_mirror)
  if not mirrored_transition
     or not valid_integer(mirrored_transition.expires_at_ms, 1, 9007199254740991)
     or (mirrored_transition.eviction_branch ~= nil
       and mirrored_transition.eviction_branch ~= 'cancel'
       and mirrored_transition.eviction_branch ~= 'unload')
     or (mirrored_transition.hard_deadline_ms ~= nil
       and (not valid_integer(
         mirrored_transition.hard_deadline_ms, 1, 9007199254740991)
         or mirrored_transition.expires_at_ms
           > mirrored_transition.hard_deadline_ms)) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='transition_mirror_invalid'})
  end
  if mirrored_transition.eviction_branch ~= nil
     or mirrored_transition.expires_at_ms > transition_now then
    return cjson.encode({status='partial_state', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='transition_key_missing_before_expiry'})
  end
end

local cached_card_queue_count = expected_revision ~= ''
  and tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1') or 0
local cached_backend_queue_counts = expected_revision ~= ''
  and decode(redis.call('HGET', KEYS[1], 'backend_queue_counts')) or {}
if expected_revision ~= '' and not initialize_domain
   and (not cached_backend_queue_counts
     or table_size(cached_backend_queue_counts) ~= table_size(known_backends)) then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='backend_queue_count_cache_invalid'})
end
local total_queue_count = cached_card_queue_count
if not valid_integer(cached_card_queue_count, 0, 10000) then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='card_queue_count_invalid'})
end
for backend_id, _ in pairs(queue_keys) do
  local expected_count = (expected_revision == '' or initialize_domain) and 0
    or (cached_backend_queue_counts and cached_backend_queue_counts[backend_id])
  if not valid_integer(expected_count, 0, 10000) then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='backend_queue_count_invalid'})
  end
  total_queue_count = total_queue_count + expected_count
end
if total_queue_count > 10000 then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='queue_domain_exceeded'})
end

local card_queue_count, card_queue_raw_count = read_queue(
  KEYS[3], now, 'card', '', known_backends, cached_card_queue_count)
if card_queue_count == nil then
  return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='card_queue_invalid'})
end
if card_queue_count > 0 then
  local current_deadline = tonumber(
    redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '0')
  local current_ready = redis.call('HGET', KEYS[1], 'bootstrap_state') == 'ready'
    and tonumber(expected_revision ~= '' and expected_revision or '0')
      < 9007199252740991
    and valid_integer(current_deadline, 1, 9007199254740991)
    and current_deadline > now and current_deadline <= now + 300000
  return cjson.encode({status='busy', ready=current_ready, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='card_queue_active'})
end
for backend_id, queue_key in pairs(queue_keys) do
  local expected_count = (expected_revision == '' or initialize_domain) and 0
    or cached_backend_queue_counts[backend_id]
  local queue_count, queue_raw_count = read_queue(
    queue_key, now, 'backend', backend_id, known_backends, expected_count)
  if queue_count == nil then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='backend_queue_invalid'})
  end
  if queue_count > 0 then
    local current_deadline = tonumber(
      redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '0')
    local current_ready = redis.call('HGET', KEYS[1], 'bootstrap_state') == 'ready'
      and tonumber(expected_revision ~= '' and expected_revision or '0')
        < 9007199252740991
      and valid_integer(current_deadline, 1, 9007199254740991)
      and current_deadline > now and current_deadline <= now + 300000
    return cjson.encode({status='busy', ready=current_ready, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='backend_queue_active'})
  end
end

if idempotent_retry then
  if current_count ~= #target_order then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='idempotent_allocation_count_changed'})
  end
  for _, backend_id in ipairs(target_order) do
    if not allocations_equal(current_by_backend[backend_id], target_by_backend[backend_id]) then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='idempotent_allocation_changed'})
    end
  end
  for backend_id, evidence in pairs(cleanup_evidence) do
    if not known_backends[backend_id] or type(evidence) ~= 'table'
       or type(evidence.lease_ids) ~= 'table' then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='cleanup_evidence_invalid'})
    end
    for _, lease_id in ipairs(evidence.lease_ids) do
      if leases_by_backend[backend_id][lease_id] then
        return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision), committed_mb=current_committed, purged_leases=0, reason='idempotent_cleanup_not_applied'})
      end
    end
  end
  local deadline = tonumber(redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '0')
  local effective_ready = redis.call('HGET', KEYS[1], 'bootstrap_state') == 'ready'
    and tonumber(expected_revision) < 9007199252740991
    and valid_integer(deadline, 1, 9007199254740991) and deadline > now
  return cjson.encode({
    status=effective_ready and 'reconciled' or 'not_ready', ready=effective_ready,
    ledger_revision=tonumber(expected_revision),
    ledger_incarnation=current_incarnation, committed_mb=current_committed,
    purged_leases=0, reason=effective_ready and '' or 'reconcile_expired',
    idempotent=true
  })
end

local purge_sets = {}
local purge_count = 0
for backend_id, evidence in pairs(cleanup_evidence) do
  if not known_backends[backend_id] or type(evidence) ~= 'table'
     or not valid_integer(evidence.observed_idle_at_ms, 1, 9007199254740991)
     or evidence.observed_idle_at_ms > now + 60000
     or type(evidence.lease_ids) ~= 'table' then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='cleanup_evidence_invalid'})
  end
  local purge_set = {}
  for _, lease_id in ipairs(evidence.lease_ids) do
    local lease = leases_by_backend[backend_id] and leases_by_backend[backend_id][lease_id]
    local allocation = current_by_backend[backend_id]
    if type(lease_id) ~= 'string' or lease_id == '' or purge_set[lease_id]
       or not lease or lease.state ~= 'stale'
       or lease.hard_deadline_ms > now
       or lease.hard_deadline_ms > evidence.observed_idle_at_ms
       or (allocation and (allocation.state == 'reserving' or allocation.state == 'loading')
           and allocation.reservation_lease_id == lease_id) then
      return cjson.encode({status='active_leases', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='lease_not_safe_to_purge'})
    end
    purge_set[lease_id] = true
    purge_count = purge_count + 1
    lease_counts[backend_id] = lease_counts[backend_id] - 1
  end
  purge_sets[backend_id] = purge_set
end

for backend_id, current in pairs(current_by_backend) do
  if not target_by_backend[backend_id] then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='allocation_removal_forbidden'})
  end
end

local committed = 0
for _, backend_id in ipairs(target_order) do
  local target = target_by_backend[backend_id]
  local current = current_by_backend[backend_id]
  if not current then
    if target.state ~= 'unknown' and target.state ~= 'unloaded'
       and target.state ~= 'cpu_fallback' then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='unsafe_bootstrap_state'})
    end
  else
    if allocation_generation_regressed(target, current) then
      return cjson.encode({status='stale_generation', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='generation_regression'})
    end
    local config_changed = target.budget_mb ~= current.budget_mb
      or target.eviction_priority ~= current.eviction_priority
      or target.evictable ~= current.evictable
      or target.max_concurrency ~= current.max_concurrency
    if config_changed and counted_states[current.state] then
      return cjson.encode({status='config_mismatch', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='counted_allocation_config_changed'})
    end
    if target.last_used_at_ms < current.last_used_at_ms then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='last_used_regression'})
    end
  end
  if target.state == 'unknown' and target.evictable then
    return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='unknown_cannot_be_evictable'})
  end
  if target.generation == cjson.null
     and (lease_counts[backend_id] or 0) > 0 then
    return cjson.encode({status='active_leases', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='null_generation_has_leases'})
  end
  if target.state == 'reserving' or target.state == 'loading' then
    local reservation = leases_by_backend[backend_id]
      and leases_by_backend[backend_id][target.reservation_lease_id]
    local purged = purge_sets[backend_id]
      and purge_sets[backend_id][target.reservation_lease_id]
    if not current or not reservation or purged
       or reservation.owner_id ~= target.reservation_owner_id
       or reservation.generation ~= target.generation then
      return cjson.encode({status='ledger_corrupt', ready=false, ledger_revision=0, committed_mb=current_committed, purged_leases=0, reason='reservation_lease_mismatch'})
    end
  end
  if not counted_states[target.state]
     and (lease_counts[backend_id] or 0) > 0 then
    return cjson.encode({status='active_leases', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), committed_mb=current_committed, purged_leases=0, reason='leases_prevent_release'})
  end
  if (lease_counts[backend_id] or 0) > target.max_concurrency then
    return cjson.encode({status='config_mismatch', ready=false, ledger_revision=tonumber(expected_revision ~= '' and expected_revision or '0'), ledger_incarnation=current_incarnation, committed_mb=current_committed, purged_leases=0, reason='lease_concurrency_exceeded'})
  end
  if counted_states[target.state] then committed = committed + target.budget_mb end
end

for backend_id, purge_set in pairs(purge_sets) do
  for lease_id, _ in pairs(purge_set) do
    redis.call('HDEL', lease_keys[backend_id], lease_id)
  end
end
for _, backend_id in ipairs(target_order) do
  redis.call('HSET', KEYS[2], backend_id, cjson.encode(target_by_backend[backend_id]))
end
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[4])
local backend_queue_counts = {}
for backend_id, queue_key in pairs(queue_keys) do
  redis.call('DEL', queue_key)
  backend_queue_counts[backend_id] = 0
end

local ready = ARGV[4] == '1'
local reason = ''
if committed > tonumber(ARGV[3]) then
  ready = false
  reason = 'committed_exceeds_allocatable'
end
local deadline = 0
if ready then deadline = requested_deadline end
local rebase_revision = expected_revision ~= ''
  and tonumber(expected_revision) >= 9007199252740990
local revision = 1
if rebase_revision then
  current_incarnation = proposed_incarnation
  redis.call('HSET', KEYS[1], 'ledger_revision', '1')
else
  revision = redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
end
redis.call('HSET', KEYS[1],
  'resource_id', ARGV[1],
  'allocatable_mb', ARGV[3],
  'bootstrap_state', ready and 'ready' or 'not_ready',
  'reconcile_deadline_ms', tostring(deadline),
  'not_ready_reason', reason,
  'ledger_version', '3',
  'ledger_incarnation', current_incarnation,
  'committed_mb', tostring(committed),
  'backend_domain', ARGV[10],
  'backend_domain_fingerprint', ARGV[11],
  'membership_domain', ARGV[12],
  'membership_domain_fingerprint', ARGV[13],
  'active_backend_domain', ARGV[14],
  'active_backend_domain_fingerprint', ARGV[15],
  'allocation_count', tostring(#target_order),
  'lease_counts', cjson.encode(lease_counts),
  'card_queue_count', '0',
  'backend_queue_counts', cjson.encode(backend_queue_counts),
  'transition_mirror', '',
  'last_repair_id', ARGV[6],
  'last_repair_fingerprint', ARGV[9],
  'last_repair_expected_revision', supplied_expected_revision,
  'last_repair_expected_incarnation', supplied_expected_incarnation,
  'last_repair_revision', tostring(revision),
  'updated_at_ms', tostring(now))
return cjson.encode({
  status=ready and 'reconciled' or 'not_ready', ready=ready,
  ledger_revision=revision, ledger_incarnation=current_incarnation,
  committed_mb=committed,
  purged_leases=purge_count, reason=reason, idempotent=idempotent_retry
})
"""
_EVOLVE_BACKEND_DOMAINS_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function valid_epoch(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function increment_epoch(value)
  local carry = 1
  local digits = {}
  for index = string.len(value), 1, -1 do
    local digit = tonumber(string.sub(value, index, index)) + carry
    if digit >= 10 then
      digit = digit - 10
      carry = 1
    else
      carry = 0
    end
    table.insert(digits, 1, tostring(digit))
  end
  if carry == 1 then table.insert(digits, 1, '1') end
  return table.concat(digits)
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_epoch(item.generation)
end
local function valid_revision(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 16 then return true end
  if string.len(value) > 16 then return false end
  return value < '9007199254740991'
end
local function validate_domains(domain_raw, membership_raw, active_raw)
  local domain = decode(domain_raw)
  local membership_domain = decode(membership_raw)
  local active_domain = decode(active_raw)
  if not domain or #domain > 64
     or not membership_domain or #membership_domain ~= #domain
     or not active_domain or #active_domain > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#membership_domain == 0 and membership_raw ~= '[]')
     or (#active_domain == 0 and active_raw ~= '[]') then
    return nil
  end
  local valid_states = {pending=true, active=true, retiring=true}
  local known = {}
  local members = {}
  local previous = nil
  local active_index = 1
  for index, backend_id in ipairs(domain) do
    local member = membership_domain[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or type(member) ~= 'table' or table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not valid_epoch(member.membership_epoch)
       or not valid_states[member.state] then
      return nil
    end
    previous = backend_id
    known[backend_id] = true
    members[backend_id] = member
    if member.state == 'active' then
      if active_domain[active_index] ~= backend_id then return nil end
      active_index = active_index + 1
    end
  end
  if active_index ~= #active_domain + 1 then return nil end
  return {domain=domain, known=known, members=members, active=active_domain}
end
local function response(status, revision, incarnation, domains, reason, idempotent)
  return cjson.encode({
    status=status, ledger_revision=revision,
    ledger_incarnation=incarnation or '',
    backend_ids=domains and domains.domain or {},
    active_backend_ids=domains and domains.active or {},
    reason=reason or '', idempotent=idempotent or false
  })
end

local incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
local current_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
local target_domains = validate_domains(ARGV[12], ARGV[14], ARGV[16])
if not target_domains then
  return response('ledger_corrupt', 0, incarnation, nil, 'target_membership_domain_invalid')
end
if redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[1] then
  return response('not_ready', 0, incarnation, target_domains, 'card_missing_or_mismatched')
end
if redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
  return response('not_ready', tonumber(current_revision or '0') or 0,
    incarnation, target_domains, 'proof_reset_in_progress')
end
if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3' then
  return response('ledger_corrupt', tonumber(current_revision or '0') or 0,
    incarnation, target_domains, 'legacy_schema_requires_proof_reset')
end
if not valid_revision(current_revision) then
  return response('ledger_corrupt', 0, incarnation, target_domains, 'ledger_revision_invalid')
end
if type(incarnation) ~= 'string' or incarnation == ''
   or string.len(incarnation) > 128 then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'ledger_incarnation_invalid')
end
if incarnation ~= ARGV[3] then
  return response('stale_revision', tonumber(current_revision), incarnation,
    target_domains, 'ledger_incarnation_changed')
end

if redis.call('HGET', KEYS[1], 'last_domain_evolution_id') == ARGV[4] then
  if redis.call('HGET', KEYS[1], 'last_domain_evolution_fingerprint') ~= ARGV[5]
     or redis.call('HGET', KEYS[1], 'last_domain_evolution_expected_revision') ~= ARGV[2]
     or redis.call('HGET', KEYS[1], 'last_domain_evolution_expected_incarnation') ~= ARGV[3] then
    return response('config_mismatch', tonumber(current_revision or '0') or 0,
      incarnation, target_domains, 'evolution_id_conflict')
  end
  local marker_revision = redis.call('HGET', KEYS[1], 'last_domain_evolution_revision')
  if not valid_revision(marker_revision) or current_revision ~= marker_revision then
    return response('stale_revision', tonumber(current_revision or '0') or 0,
      incarnation, target_domains, 'ledger_changed_after_evolution')
  end
  if redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[12]
     or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[13]
     or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[14]
     or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[15]
     or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[16]
     or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[17] then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'evolution_marker_domain_mismatch')
  end
  return response('evolved', tonumber(current_revision), incarnation,
    target_domains, '', true)
end

if current_revision ~= ARGV[2] then
  return response('stale_revision', tonumber(current_revision), incarnation,
    target_domains, 'ledger_revision_changed')
end
if tonumber(current_revision) >= 9007199252740991 then
  return response('not_ready', tonumber(current_revision), incarnation,
    target_domains, 'ledger_revision_rebase_required')
end
if redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'not_ready'
   or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= '0' then
  return response('not_ready', tonumber(current_revision), incarnation,
    target_domains, 'domain_evolution_requires_not_ready')
end
if redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[6]
   or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[7]
   or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[8]
   or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[9]
   or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[10]
   or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[11] then
  return response('stale_revision', tonumber(current_revision), incarnation,
    target_domains, 'membership_domain_changed')
end
local current_domains = validate_domains(ARGV[6], ARGV[8], ARGV[10])
if not current_domains then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'current_membership_domain_invalid')
end
if #KEYS ~= 4 + (#target_domains.domain * 2)
   or #ARGV ~= 17 + #target_domains.domain then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'backend_domain_key_mismatch')
end
local lease_keys = {}
local queue_keys = {}
for index, backend_id in ipairs(target_domains.domain) do
  if ARGV[index + 17] ~= backend_id then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'backend_domain_key_mismatch')
  end
  lease_keys[backend_id] = KEYS[3 + (index * 2)]
  queue_keys[backend_id] = KEYS[4 + (index * 2)]
end

for _, backend_id in ipairs(current_domains.domain) do
  local current = current_domains.members[backend_id]
  local target = target_domains.members[backend_id]
  if not target then
    return response('config_mismatch', tonumber(current_revision), incarnation,
      target_domains, 'backend_domain_shrink_forbidden')
  end
  local same_epoch = target.membership_epoch == current.membership_epoch
  local next_epoch = not same_epoch
    and target.membership_epoch == increment_epoch(current.membership_epoch)
  local valid_transition = same_epoch and (
      target.state == current.state
      or (current.state == 'pending' and target.state == 'active'))
    or next_epoch and (
      (current.state == 'pending' and target.state == 'pending')
      or (current.state ~= 'retiring' and target.state == 'retiring'))
  if not valid_transition then
    return response('config_mismatch', tonumber(current_revision), incarnation,
      target_domains, 'membership_transition_invalid')
  end
end
local new_backends = {}
for _, backend_id in ipairs(target_domains.domain) do
  if not current_domains.known[backend_id] then
    new_backends[backend_id] = true
    local member = target_domains.members[backend_id]
    if member.state ~= 'pending' or member.membership_epoch ~= '1' then
      return response('config_mismatch', tonumber(current_revision), incarnation,
        target_domains, 'new_membership_must_start_pending')
    end
    if redis.call('HEXISTS', KEYS[2], backend_id) ~= 0
       or redis.call('HLEN', lease_keys[backend_id]) ~= 0
       or redis.call('LLEN', queue_keys[backend_id]) ~= 0 then
      return response('partial_state', tonumber(current_revision), incarnation,
        target_domains, 'new_backend_children_present')
    end
  end
end

local lease_counts = decode(redis.call('HGET', KEYS[1], 'lease_counts'))
local backend_queue_counts = decode(redis.call('HGET', KEYS[1], 'backend_queue_counts'))
if not lease_counts or not backend_queue_counts
   or table_size(lease_counts) ~= #current_domains.domain
   or table_size(backend_queue_counts) ~= #current_domains.domain then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'child_count_cache_invalid')
end
local total_queue_count = 0
for _, backend_id in ipairs(current_domains.domain) do
  local lease_count = redis.call('HLEN', lease_keys[backend_id])
  local backend_queue_count = redis.call('LLEN', queue_keys[backend_id])
  if lease_count > 10000 then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'lease_domain_exceeded')
  end
  if backend_queue_count > 10000 then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'queue_domain_exceeded')
  end
  if lease_counts[backend_id] ~= lease_count
     or backend_queue_counts[backend_id] ~= backend_queue_count then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'child_count_cache_drift')
  end
  total_queue_count = total_queue_count + backend_queue_count
end
local card_queue_count = redis.call('LLEN', KEYS[3])
if tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1') ~= card_queue_count then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'card_queue_count_cache_drift')
end
total_queue_count = total_queue_count + card_queue_count
if card_queue_count > 10000 or total_queue_count > 10000 then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'queue_domain_exceeded')
end
local card_queue_entries = redis.call('LRANGE', KEYS[3], 0, -1)
for _, raw in ipairs(card_queue_entries) do
  local ticket = decode(raw)
  if not ticket or type(ticket.backend_id) ~= 'string'
     or not target_domains.known[ticket.backend_id] then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'card_queue_domain_invalid')
  end
  if new_backends[ticket.backend_id] then
    return response('partial_state', tonumber(current_revision), incarnation,
      target_domains, 'new_backend_children_present')
  end
end
local allocation_count = redis.call('HLEN', KEYS[2])
if tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1') ~= allocation_count
   or allocation_count > 64 then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'allocation_count_cache_drift')
end
local counted_states = {
  unknown=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true
}
local valid_states = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
local committed = 0
local allocation_entries = redis.call('HGETALL', KEYS[2])
for index = 1, #allocation_entries, 2 do
  local backend_id = allocation_entries[index]
  local allocation = decode(allocation_entries[index + 1])
  local reservation_state = allocation
    and (allocation.state == 'reserving' or allocation.state == 'loading')
  local has_reservation = allocation
    and type(allocation.reservation_lease_id) == 'string'
    and allocation.reservation_lease_id ~= ''
    and type(allocation.reservation_owner_id) == 'string'
    and allocation.reservation_owner_id ~= ''
  local reservation_present = allocation
    and (allocation.reservation_lease_id ~= nil
      or allocation.reservation_owner_id ~= nil)
  if not current_domains.known[backend_id] or not allocation
     or allocation.backend_id ~= backend_id or not valid_states[allocation.state]
     or type(allocation.budget_mb) ~= 'number' or allocation.budget_mb < 1
     or allocation.budget_mb > 9007199254740991
     or allocation.budget_mb ~= math.floor(allocation.budget_mb)
     or not valid_allocation_generation(allocation)
     or type(allocation.eviction_priority) ~= 'number'
     or allocation.eviction_priority ~= math.floor(allocation.eviction_priority)
     or math.abs(allocation.eviction_priority) > 9007199254740991
     or type(allocation.evictable) ~= 'boolean'
     or type(allocation.max_concurrency) ~= 'number'
     or allocation.max_concurrency ~= math.floor(allocation.max_concurrency)
     or allocation.max_concurrency < 1 or allocation.max_concurrency > 10000
     or type(allocation.last_used_at_ms) ~= 'number'
     or allocation.last_used_at_ms ~= math.floor(allocation.last_used_at_ms)
     or allocation.last_used_at_ms < 1
     or allocation.last_used_at_ms > 9007199254740991
     or type(allocation.not_evict_before_ms) ~= 'number'
     or allocation.not_evict_before_ms ~= math.floor(allocation.not_evict_before_ms)
     or allocation.not_evict_before_ms < 0
     or allocation.not_evict_before_ms > 9007199254740991
     or (allocation.state == 'resident' and allocation.not_evict_before_ms == 0)
     or (reservation_state and not has_reservation)
     or (not reservation_state and reservation_present) then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'allocation_domain_invalid')
  end
  if allocation.generation == cjson.null
     and lease_counts[backend_id] > 0 then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'null_generation_has_leases')
  end
  if counted_states[allocation.state] then committed = committed + allocation.budget_mb end
end
if tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1') ~= committed then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'committed_cache_drift')
end
local transition_raw = redis.call('GET', KEYS[4])
local transition_mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
if transition_mirror == false or (transition_raw and transition_raw ~= transition_mirror) then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'transition_mirror_mismatch')
end
if transition_raw then
  local transition = decode(transition_raw)
  if not transition or not current_domains.known[transition.backend_id]
     or (transition.eviction_branch ~= nil
       and transition.eviction_branch ~= 'cancel'
       and transition.eviction_branch ~= 'unload') then
    return response('ledger_corrupt', tonumber(current_revision), incarnation,
      target_domains, 'transition_domain_invalid')
  end
elseif transition_mirror ~= '' then
  return response('ledger_corrupt', tonumber(current_revision), incarnation,
    target_domains, 'transition_key_missing')
end

if ARGV[6] == ARGV[12] and ARGV[8] == ARGV[14] and ARGV[10] == ARGV[16]
   and ARGV[7] == ARGV[13] and ARGV[9] == ARGV[15] and ARGV[11] == ARGV[17] then
  return response('unchanged', tonumber(current_revision), incarnation,
    target_domains, '', true)
end
for _, backend_id in ipairs(target_domains.domain) do
  if not current_domains.known[backend_id] then
    lease_counts[backend_id] = 0
    backend_queue_counts[backend_id] = 0
  end
end
local revision = redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
redis.call('HSET', KEYS[1],
  'bootstrap_state', 'not_ready',
  'reconcile_deadline_ms', '0',
  'not_ready_reason', 'backend_domain_evolved',
  'backend_domain', ARGV[12],
  'backend_domain_fingerprint', ARGV[13],
  'membership_domain', ARGV[14],
  'membership_domain_fingerprint', ARGV[15],
  'active_backend_domain', ARGV[16],
  'active_backend_domain_fingerprint', ARGV[17],
  'lease_counts', cjson.encode(lease_counts),
  'backend_queue_counts', cjson.encode(backend_queue_counts),
  'last_domain_evolution_id', ARGV[4],
  'last_domain_evolution_fingerprint', ARGV[5],
  'last_domain_evolution_expected_revision', ARGV[2],
  'last_domain_evolution_expected_incarnation', ARGV[3],
  'last_domain_evolution_revision', tostring(revision),
  'updated_at_ms', tostring(now_ms()))
return response('evolved', revision, incarnation, target_domains, '', false)
"""
_COLLECT_RETIRED_BACKEND_LUA = r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function valid_positive(value, maximum)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < string.len(maximum) then return true end
  if string.len(value) > string.len(maximum) then return false end
  return value <= maximum
end
local function valid_fingerprint(value)
  return type(value) == 'string' and string.len(value) == 64
    and string.match(value, '^[0-9a-f]+$') ~= nil
end
local function key_type(key)
  local reply = redis.call('TYPE', key)
  if type(reply) == 'table' then return reply.ok end
  return reply
end
local function response(status, reason, revision, incarnation, idempotent)
  return cjson.encode({
    status=status, reason=reason or '',
    ledger_revision=tonumber(revision or '0') or 0,
    ledger_incarnation=incarnation or '',
    idempotent=idempotent or false
  })
end
local function validate_domains(domain_raw, membership_raw, active_raw)
  local domain = decode(domain_raw)
  local memberships = decode(membership_raw)
  local active = decode(active_raw)
  if not domain or not memberships or not active
     or #domain > 64 or #memberships ~= #domain or #active > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#memberships == 0 and membership_raw ~= '[]')
     or (#active == 0 and active_raw ~= '[]') then
    return nil
  end
  local known = {}
  local members = {}
  local previous = nil
  local active_index = 1
  local valid_states = {pending=true, active=true, retiring=true}
  for index, backend_id in ipairs(domain) do
    local member = memberships[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or type(member) ~= 'table' or table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not valid_positive(member.membership_epoch, '9223372036854775807')
       or not valid_states[member.state] then
      return nil
    end
    previous = backend_id
    known[backend_id] = true
    members[backend_id] = member
    if member.state == 'active' then
      if active[active_index] ~= backend_id then return nil end
      active_index = active_index + 1
    end
  end
  if active_index ~= #active + 1 then return nil end
  return {domain=domain, active=active, known=known, members=members}
end
local function card_queue_excludes_backend(queue_key, backend_id)
  local entries = redis.call('LRANGE', queue_key, 0, -1)
  if #entries > 10000 then return nil, 'queue_domain_exceeded' end
  for _, raw in ipairs(entries) do
    local ticket = decode(raw)
    if not ticket or type(ticket.backend_id) ~= 'string' then
      return nil, 'card_queue_domain_invalid'
    end
    if ticket.backend_id == backend_id then
      return nil, 'card_queue_not_empty'
    end
  end
  return #entries, nil
end

if #ARGV ~= 23 then
  return response('ledger_corrupt', 'collection_argument_domain_invalid')
end
if not valid_positive(ARGV[3], '9223372036854775807')
   or not valid_positive(ARGV[4], '9007199254740991')
   or not valid_positive(ARGV[8], '9007199254740991')
   or not valid_positive(ARGV[9], '9007199254740991')
   or not valid_positive(ARGV[23], '2147483647') then
  return response('ledger_corrupt', 'collection_numeric_input_invalid')
end
if type(ARGV[2]) ~= 'string' or ARGV[2] == '' or string.len(ARGV[2]) > 128
   or type(ARGV[5]) ~= 'string' or ARGV[5] == '' or string.len(ARGV[5]) > 128
   or type(ARGV[6]) ~= 'string' or ARGV[6] == '' or string.len(ARGV[6]) > 256
   or not valid_fingerprint(ARGV[7])
   or type(ARGV[22]) ~= 'string' or string.len(ARGV[22]) ~= 36
   or not string.match(ARGV[22], '^[0-9a-f%-]+$') then
  return response('ledger_corrupt', 'collection_identity_input_invalid')
end
for index = 11, 21, 2 do
  if not valid_fingerprint(ARGV[index]) then
    return response('ledger_corrupt', 'collection_domain_fingerprint_invalid')
  end
end
local current_domains = validate_domains(ARGV[10], ARGV[12], ARGV[14])
local target_domains = validate_domains(ARGV[16], ARGV[18], ARGV[20])
if not current_domains or not target_domains then
  return response('ledger_corrupt', 'collection_membership_domain_invalid')
end
if #KEYS ~= 5 + (#current_domains.domain * 2) then
  return response('ledger_corrupt', 'collection_argument_domain_invalid')
end
local current_member = current_domains.members[ARGV[2]]
if not current_member or current_member.state ~= 'retiring'
   or current_member.membership_epoch ~= ARGV[3] then
  return response('config_mismatch', 'collection_target_not_exact_retiring')
end
if target_domains.known[ARGV[2]] or #target_domains.domain ~= #current_domains.domain - 1
   or ARGV[14] ~= ARGV[20] or ARGV[15] ~= ARGV[21] then
  return response('config_mismatch', 'collection_target_domain_invalid')
end
for _, backend_id in ipairs(current_domains.domain) do
  if backend_id ~= ARGV[2] then
    local before = current_domains.members[backend_id]
    local after = target_domains.members[backend_id]
    if not after or before.membership_epoch ~= after.membership_epoch
       or before.state ~= after.state then
      return response('config_mismatch', 'collection_target_domain_invalid')
    end
  end
end

local function validate_runtime_state(ledger_domains, key_domains, target_id, post)
  if (key_type(KEYS[2]) ~= 'hash' and key_type(KEYS[2]) ~= 'none')
     or (key_type(KEYS[3]) ~= 'list' and key_type(KEYS[3]) ~= 'none')
     or (key_type(KEYS[4]) ~= 'string' and key_type(KEYS[4]) ~= 'none') then
    return nil, 'ledger_corrupt', 'runtime_key_type_invalid'
  end
  local allocations = {}
  local allocation_entries = redis.call('HGETALL', KEYS[2])
  if #allocation_entries > 128 then
    return nil, 'ledger_corrupt', 'allocation_domain_exceeded'
  end
  local allocation_count = 0
  local committed = 0
  local target_budget = 0
  for index = 1, #allocation_entries, 2 do
    local backend_id = allocation_entries[index]
    local allocation = decode(allocation_entries[index + 1])
    if not ledger_domains.known[backend_id]
       or not integrity_valid_allocation(allocation, backend_id) then
      return nil, 'ledger_corrupt', 'allocation_invalid'
    end
    if allocation.state ~= 'unknown' and allocation.state ~= 'resident' then
      return nil, 'blocked', 'allocation_not_canonical'
    end
    if backend_id == target_id then
      if post
         or allocation.state ~= 'unknown'
         or allocation.generation ~= cjson.null
         or allocation.evictable ~= false
         or allocation.budget_mb ~= tonumber(ARGV[9]) then
        return nil, 'blocked', 'allocation_not_collectable'
      end
      target_budget = allocation.budget_mb
    end
    allocations[backend_id] = allocation
    allocation_count = allocation_count + 1
    if integrity_counted_states[allocation.state] then
      committed = committed + allocation.budget_mb
    end
  end
  if tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1')
       ~= allocation_count
     or tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1')
       ~= committed then
    return nil, 'ledger_corrupt', 'allocation_cache_drift'
  end

  local lease_counts = decode(redis.call('HGET', KEYS[1], 'lease_counts'))
  local queue_counts = decode(redis.call('HGET', KEYS[1], 'backend_queue_counts'))
  if not lease_counts or not queue_counts
     or table_size(lease_counts) ~= #ledger_domains.domain
     or table_size(queue_counts) ~= #ledger_domains.domain then
    return nil, 'ledger_corrupt', 'child_count_cache_invalid'
  end
  local total_queue_count = 0
  local total_lease_count = 0
  local seen_tickets = {}
  for index, backend_id in ipairs(key_domains.domain) do
    local lease_key = KEYS[3 + (index * 2)]
    local queue_key = KEYS[4 + (index * 2)]
    if (key_type(lease_key) ~= 'hash' and key_type(lease_key) ~= 'none')
       or (key_type(queue_key) ~= 'list' and key_type(queue_key) ~= 'none') then
      return nil, 'ledger_corrupt', 'child_key_type_invalid'
    end
    local lease_entries = redis.call('HGETALL', lease_key)
    local lease_count = #lease_entries / 2
    if lease_count > 10000 then
      return nil, 'ledger_corrupt', 'lease_domain_exceeded'
    end
    for item = 1, #lease_entries, 2 do
      local lease = decode(lease_entries[item + 1])
      if not integrity_valid_lease(lease, lease_entries[item], backend_id) then
        return nil, 'ledger_corrupt', 'lease_invalid'
      end
    end
    total_lease_count = total_lease_count + lease_count
    local queue_entries = redis.call('LRANGE', queue_key, 0, -1)
    if #queue_entries > 10000 then
      return nil, 'ledger_corrupt', 'queue_domain_exceeded'
    end
    for _, raw in ipairs(queue_entries) do
      local ticket = decode(raw)
      if not integrity_valid_ticket(
          ticket, 'backend', backend_id, ledger_domains.known)
         or seen_tickets[ticket.ticket_id] then
        return nil, 'ledger_corrupt', 'backend_queue_invalid'
      end
      seen_tickets[ticket.ticket_id] = true
    end
    if backend_id == target_id then
      if lease_count ~= 0 then return nil, 'blocked', 'lease_not_empty' end
      if #queue_entries ~= 0 then
        return nil, 'blocked', 'backend_queue_not_empty'
      end
      if post then
        if lease_counts[backend_id] ~= nil or queue_counts[backend_id] ~= nil then
          return nil, 'ledger_corrupt', 'collected_child_cache_present'
        end
      elseif lease_counts[backend_id] ~= 0 or queue_counts[backend_id] ~= 0 then
        return nil, 'ledger_corrupt', 'child_count_cache_drift'
      end
    elseif lease_counts[backend_id] ~= lease_count
       or queue_counts[backend_id] ~= #queue_entries then
      return nil, 'ledger_corrupt', 'child_count_cache_drift'
    end
    if lease_count > 0 and not allocations[backend_id] then
      return nil, 'ledger_corrupt', 'lease_without_allocation'
    end
    if lease_count > 0 and allocations[backend_id].generation == cjson.null then
      return nil, 'ledger_corrupt', 'null_generation_has_leases'
    end
    total_queue_count = total_queue_count + #queue_entries
    if total_lease_count + total_queue_count > 10000 then
      return nil, 'ledger_corrupt', 'child_domain_exceeded'
    end
  end

  local card_entries = redis.call('LRANGE', KEYS[3], 0, -1)
  if #card_entries > 10000 then
    return nil, 'ledger_corrupt', 'queue_domain_exceeded'
  end
  for _, raw in ipairs(card_entries) do
    local ticket = decode(raw)
    if not integrity_valid_ticket(ticket, 'card', '', ledger_domains.known)
       or seen_tickets[ticket.ticket_id] then
      return nil, 'ledger_corrupt', 'card_queue_invalid'
    end
    if ticket.backend_id == target_id then
      return nil, 'blocked', 'card_queue_not_empty'
    end
    seen_tickets[ticket.ticket_id] = true
  end
  total_queue_count = total_queue_count + #card_entries
  if total_lease_count + total_queue_count > 10000
     or tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1')
       ~= #card_entries then
    return nil, 'ledger_corrupt', 'queue_count_cache_drift'
  end

  local transition_raw = redis.call('GET', KEYS[4])
  local transition_mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
  if transition_mirror == false
     or (transition_raw and transition_raw ~= transition_mirror)
     or (not transition_raw and transition_mirror ~= '') then
    return nil, 'ledger_corrupt', 'transition_mirror_mismatch'
  end
  if transition_raw then
    local transition = decode(transition_raw)
    if not transition
       or not ledger_domains.known[transition.backend_id]
       or type(transition.owner_id) ~= 'string' or transition.owner_id == ''
       or not integrity_valid_generation(transition.generation)
       or type(transition.operation) ~= 'string' or transition.operation == ''
       or type(transition.require_idle) ~= 'boolean'
       or (transition.eviction_branch ~= nil
         and transition.eviction_branch ~= 'cancel'
         and transition.eviction_branch ~= 'unload')
       or not integrity_valid_integer(
            transition.created_at_ms, 1, 9007199254740991)
       or not integrity_valid_integer(
            transition.expires_at_ms, 1, 9007199254740991)
       or transition.backend_id == target_id then
      return nil, 'ledger_corrupt', 'transition_invalid'
    end
  end
  return {
    allocation_count=allocation_count,
    committed=committed,
    target_budget=target_budget,
    lease_counts=lease_counts,
    queue_counts=queue_counts
  }, nil, nil
end

if key_type(KEYS[1]) ~= 'hash'
   or redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[1] then
  return response('not_ready', 'card_missing_or_mismatched')
end
local revision = redis.call('HGET', KEYS[1], 'ledger_revision')
local incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
if redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
  return response('not_ready', 'proof_reset_in_progress', revision, incarnation)
end
if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3'
   or not valid_positive(revision or '', '9007199254740991')
   or incarnation == '' or string.len(incarnation) > 128
   or redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'not_ready'
   or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= '0' then
  return response('ledger_corrupt', 'collection_card_schema_invalid', revision, incarnation)
end
local receipt_key = KEYS[#KEYS]
if key_type(receipt_key) ~= 'string' and key_type(receipt_key) ~= 'none' then
  return response('ledger_corrupt', 'collection_receipt_key_invalid', revision, incarnation)
end
local receipt_raw = redis.call('GET', receipt_key)
if receipt_raw then
  local receipt = decode(receipt_raw)
  if not receipt
     or receipt.schema ~= 'gpu-arbiter-tombstone-gc-receipt/v1'
     or receipt.resource_id ~= ARGV[1]
     or receipt.backend_id ~= ARGV[2]
     or receipt.membership_epoch ~= ARGV[3]
     or receipt.retirement_id ~= ARGV[22]
     or type(receipt.ledger_incarnation) ~= 'string'
     or receipt.ledger_incarnation == ''
     or string.len(receipt.ledger_incarnation) > 128 then
    return response('config_mismatch', 'collection_receipt_conflict', revision, incarnation)
  end
  if receipt.ledger_incarnation ~= incarnation then
    redis.call('DEL', receipt_key)
  else
    if receipt.collection_id ~= ARGV[6]
       or receipt.collection_fingerprint ~= ARGV[7]
       or receipt.target_backend_domain ~= ARGV[16]
       or receipt.target_backend_domain_fingerprint ~= ARGV[17]
       or receipt.target_membership_domain ~= ARGV[18]
       or receipt.target_membership_domain_fingerprint ~= ARGV[19]
       or receipt.target_active_backend_domain ~= ARGV[20]
       or receipt.target_active_backend_domain_fingerprint ~= ARGV[21] then
      return response('config_mismatch', 'collection_receipt_conflict', revision, incarnation)
    end
    if redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[16]
       or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[17]
       or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[18]
       or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[19]
       or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[20]
       or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[21] then
      return response('stale_revision', 'ledger_changed_after_collection', revision, incarnation)
    end
    local state, state_status, state_reason = validate_runtime_state(
      target_domains, current_domains, ARGV[2], true)
    if not state then
      return response(state_status, state_reason, revision, incarnation)
    end
    return response('collected', '', revision, incarnation, true)
  end
end

if tonumber(revision) >= 9007199252740991 then
  return response(
    'not_ready', 'ledger_revision_rebase_required', revision, incarnation)
end
if revision ~= ARGV[4] or incarnation ~= ARGV[5] then
  return response('stale_revision', 'ledger_changed', revision, incarnation)
end
if redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[10]
   or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[11]
   or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[12]
   or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[13]
   or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[14]
   or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[15] then
  return response('stale_revision', 'membership_domain_changed', revision, incarnation)
end
local now = now_ms()
local deadline = tonumber(ARGV[8])
if deadline <= now or deadline > now + 300000 then
  return response('blocked', 'live_proof_expired', revision, incarnation)
end
local state, state_status, state_reason = validate_runtime_state(
  current_domains, current_domains, ARGV[2], false)
if not state then
  return response(state_status, state_reason, revision, incarnation)
end

local next_revision = tonumber(revision) + 1
local receipt = cjson.encode({
  schema='gpu-arbiter-tombstone-gc-receipt/v1',
  resource_id=ARGV[1], backend_id=ARGV[2], membership_epoch=ARGV[3],
  retirement_id=ARGV[22], ledger_incarnation=incarnation,
  collection_id=ARGV[6], collection_fingerprint=ARGV[7],
  result_revision=tostring(next_revision), collected_at_ms=tostring(now),
  target_backend_domain=ARGV[16],
  target_backend_domain_fingerprint=ARGV[17],
  target_membership_domain=ARGV[18],
  target_membership_domain_fingerprint=ARGV[19],
  target_active_backend_domain=ARGV[20],
  target_active_backend_domain_fingerprint=ARGV[21]
})
if not redis.call('SET', receipt_key, receipt, 'PX', ARGV[23], 'NX') then
  return response('config_mismatch', 'collection_receipt_conflict', revision, incarnation)
end

local target_index = nil
for index, backend_id in ipairs(current_domains.domain) do
  if backend_id == ARGV[2] then target_index = index end
end
local target_lease_key = KEYS[3 + (target_index * 2)]
local target_queue_key = KEYS[4 + (target_index * 2)]
redis.call('HDEL', KEYS[2], ARGV[2])
redis.call('DEL', target_lease_key, target_queue_key)
state.lease_counts[ARGV[2]] = nil
state.queue_counts[ARGV[2]] = nil
local next_lease_counts = #target_domains.domain == 0
  and '{}' or cjson.encode(state.lease_counts)
local next_queue_counts = #target_domains.domain == 0
  and '{}' or cjson.encode(state.queue_counts)
redis.call('HSET', KEYS[1],
  'ledger_revision', tostring(next_revision),
  'bootstrap_state', 'not_ready',
  'reconcile_deadline_ms', '0',
  'not_ready_reason', 'tombstone_collected_pending_db',
  'backend_domain', ARGV[16],
  'backend_domain_fingerprint', ARGV[17],
  'membership_domain', ARGV[18],
  'membership_domain_fingerprint', ARGV[19],
  'active_backend_domain', ARGV[20],
  'active_backend_domain_fingerprint', ARGV[21],
  'committed_mb', tostring(state.committed - state.target_budget),
  'allocation_count', tostring(state.allocation_count - (state.target_budget > 0 and 1 or 0)),
  'lease_counts', next_lease_counts,
  'backend_queue_counts', next_queue_counts,
  'updated_at_ms', tostring(now))
return response('collected', '', next_revision, incarnation, false)
"""
_VERIFY_TOMBSTONE_GC_LUA = r"""
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function valid_positive(value, maximum)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < string.len(maximum) then return true end
  if string.len(value) > string.len(maximum) then return false end
  return value <= maximum
end
local function valid_fingerprint(value)
  return type(value) == 'string' and string.len(value) == 64
    and string.match(value, '^[0-9a-f]+$') ~= nil
end
local function response(status, reason, revision, incarnation, fingerprint)
  return cjson.encode({
    status=status, reason=reason or '',
    ledger_revision=tonumber(revision or '0') or 0,
    ledger_incarnation=incarnation or '',
    fingerprint=fingerprint or ''
  })
end
local function validate_domains(domain_raw, membership_raw, active_raw)
  local domain = decode(domain_raw)
  local memberships = decode(membership_raw)
  local active = decode(active_raw)
  if not domain or not memberships or not active
     or #domain > 64 or #memberships ~= #domain or #active > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#memberships == 0 and membership_raw ~= '[]')
     or (#active == 0 and active_raw ~= '[]') then
    return nil
  end
  local previous = nil
  local known = {}
  local members = {}
  local active_index = 1
  local valid_states = {pending=true, active=true, retiring=true}
  for index, backend_id in ipairs(domain) do
    local member = memberships[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous and backend_id <= previous)
       or type(member) ~= 'table' or table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not valid_positive(member.membership_epoch, '9223372036854775807')
       or not valid_states[member.state] then
      return nil
    end
    previous = backend_id
    known[backend_id] = true
    members[backend_id] = member
    if member.state == 'active' then
      if active[active_index] ~= backend_id then return nil end
      active_index = active_index + 1
    end
  end
  if active_index ~= #active + 1 then return nil end
  return {domain=domain, known=known, members=members}
end

if #ARGV ~= 17
   or not valid_positive(ARGV[3], '9223372036854775807')
   or type(ARGV[10]) ~= 'string' or string.len(ARGV[10]) ~= 36
   or not string.match(ARGV[10], '^[0-9a-f%-]+$') then
  return response('invalid', 'receipt_arguments_invalid')
end
for index = 5, 9, 2 do
  if not valid_fingerprint(ARGV[index]) then
    return response('invalid', 'receipt_domain_fingerprint_invalid')
  end
end
for index = 12, 16, 2 do
  if not valid_fingerprint(ARGV[index]) then
    return response('invalid', 'receipt_domain_fingerprint_invalid')
  end
end
local target = validate_domains(ARGV[4], ARGV[6], ARGV[8])
local current = validate_domains(ARGV[11], ARGV[13], ARGV[15])
local key_domain = decode(ARGV[17])
-- Current and receipt domains are each capped at 64; their safe union can be 128.
if not target or not current or target.known[ARGV[2]]
   or not current.known[ARGV[2]]
   or current.members[ARGV[2]].state ~= 'retiring'
   or current.members[ARGV[2]].membership_epoch ~= ARGV[3]
   or not key_domain or #key_domain > 128
   or (#key_domain == 0 and ARGV[17] ~= '[]')
   or #KEYS ~= 5 + (#key_domain * 2) then
  return response('invalid', 'receipt_target_domain_invalid')
end
local key_known = {}
local previous_key_id = nil
for _, backend_id in ipairs(key_domain) do
  if type(backend_id) ~= 'string' or backend_id == ''
     or string.len(backend_id) > 128
     or (previous_key_id and backend_id <= previous_key_id) then
    return response('invalid', 'receipt_key_domain_invalid')
  end
  previous_key_id = backend_id
  key_known[backend_id] = true
end
for _, backend_id in ipairs(target.domain) do
  if not key_known[backend_id] then
    return response('invalid', 'receipt_key_domain_invalid')
  end
end
for _, backend_id in ipairs(current.domain) do
  if not key_known[backend_id] then
    return response('invalid', 'receipt_key_domain_invalid')
  end
end
if redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[1] then
  return response('missing', 'receipt_card_missing')
end
local revision = redis.call('HGET', KEYS[1], 'ledger_revision')
local incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation') or ''
if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3'
   or redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared'
   or redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'not_ready'
   or redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') ~= '0'
   or not valid_positive(revision or '', '9007199254740991')
   or incarnation == '' or string.len(incarnation) > 128
   or redis.call('HGET', KEYS[1], 'backend_domain') ~= ARGV[4]
   or redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= ARGV[5]
   or redis.call('HGET', KEYS[1], 'membership_domain') ~= ARGV[6]
   or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= ARGV[7]
   or redis.call('HGET', KEYS[1], 'active_backend_domain') ~= ARGV[8]
   or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= ARGV[9] then
  return response('missing', 'receipt_marker_missing_or_stale', revision, incarnation)
end
local receipt_raw = redis.call('GET', KEYS[#KEYS])
local receipt = decode(receipt_raw)
if not receipt
   or receipt.schema ~= 'gpu-arbiter-tombstone-gc-receipt/v1'
   or receipt.resource_id ~= ARGV[1]
   or receipt.backend_id ~= ARGV[2]
   or receipt.membership_epoch ~= ARGV[3]
   or receipt.retirement_id ~= ARGV[10]
   or receipt.ledger_incarnation ~= incarnation
   or not valid_fingerprint(receipt.collection_fingerprint or '')
   or receipt.target_backend_domain ~= ARGV[4]
   or receipt.target_backend_domain_fingerprint ~= ARGV[5]
   or receipt.target_membership_domain ~= ARGV[6]
   or receipt.target_membership_domain_fingerprint ~= ARGV[7]
   or receipt.target_active_backend_domain ~= ARGV[8]
   or receipt.target_active_backend_domain_fingerprint ~= ARGV[9] then
  return response('missing', 'receipt_marker_missing_or_stale', revision, incarnation)
end

local allocation_entries = redis.call('HGETALL', KEYS[2])
local allocation_count = 0
local committed = 0
local allocations = {}
if #allocation_entries > 128 then
  return response('invalid', 'receipt_allocation_invalid', revision, incarnation)
end
for index = 1, #allocation_entries, 2 do
  local backend_id = allocation_entries[index]
  local allocation = decode(allocation_entries[index + 1])
  if not target.known[backend_id]
     or not integrity_valid_allocation(allocation, backend_id) then
    return response('invalid', 'receipt_allocation_invalid', revision, incarnation)
  end
  allocations[backend_id] = allocation
  allocation_count = allocation_count + 1
  if integrity_counted_states[allocation.state] then
    committed = committed + allocation.budget_mb
  end
end
if tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1')
     ~= allocation_count
   or tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1')
     ~= committed then
  return response('invalid', 'receipt_allocation_cache_invalid', revision, incarnation)
end

local lease_counts = decode(redis.call('HGET', KEYS[1], 'lease_counts'))
local queue_counts = decode(redis.call('HGET', KEYS[1], 'backend_queue_counts'))
if not lease_counts or not queue_counts
   or lease_counts[ARGV[2]] ~= nil or queue_counts[ARGV[2]] ~= nil
   or table_size(lease_counts) ~= #target.domain
   or table_size(queue_counts) ~= #target.domain then
  return response('invalid', 'receipt_child_cache_invalid', revision, incarnation)
end
local total_queue_count = 0
local total_lease_count = 0
local seen_tickets = {}
for index, backend_id in ipairs(key_domain) do
  local lease_key = KEYS[3 + (index * 2)]
  local queue_key = KEYS[4 + (index * 2)]
  local lease_entries = redis.call('HGETALL', lease_key)
  local queue_entries = redis.call('LRANGE', queue_key, 0, -1)
  local lease_count = #lease_entries / 2
  if lease_count > 10000 or #queue_entries > 10000 then
    return response('invalid', 'receipt_child_domain_exceeded', revision, incarnation)
  end
  if target.known[backend_id] then
    for item = 1, #lease_entries, 2 do
      local lease = decode(lease_entries[item + 1])
      if not integrity_valid_lease(lease, lease_entries[item], backend_id) then
        return response('invalid', 'receipt_lease_invalid', revision, incarnation)
      end
    end
    for _, raw in ipairs(queue_entries) do
      local ticket = decode(raw)
      if not integrity_valid_ticket(ticket, 'backend', backend_id, target.known)
         or seen_tickets[ticket.ticket_id] then
        return response('invalid', 'receipt_backend_queue_invalid', revision, incarnation)
      end
      seen_tickets[ticket.ticket_id] = true
    end
  elseif lease_count ~= 0 or #queue_entries ~= 0
     or lease_counts[backend_id] ~= nil or queue_counts[backend_id] ~= nil then
    return response('invalid', 'receipt_child_reappeared', revision, incarnation)
  end
  total_lease_count = total_lease_count + lease_count
  if target.known[backend_id]
     and (lease_counts[backend_id] ~= lease_count
     or queue_counts[backend_id] ~= #queue_entries
     or (lease_count > 0 and not allocations[backend_id])
     or (lease_count > 0 and allocations[backend_id].generation == cjson.null)) then
    return response('invalid', 'receipt_child_cache_invalid', revision, incarnation)
  end
  total_queue_count = total_queue_count + #queue_entries
  if total_lease_count + total_queue_count > 10000 then
    return response('invalid', 'receipt_child_domain_exceeded', revision, incarnation)
  end
end
local card_entries = redis.call('LRANGE', KEYS[3], 0, -1)
if #card_entries > 10000
   or tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1')
        ~= #card_entries then
  return response('invalid', 'receipt_card_queue_invalid', revision, incarnation)
end
for _, raw in ipairs(card_entries) do
  local ticket = decode(raw)
  if not integrity_valid_ticket(ticket, 'card', '', target.known)
     or seen_tickets[ticket.ticket_id] then
    return response('invalid', 'receipt_card_queue_invalid', revision, incarnation)
  end
  seen_tickets[ticket.ticket_id] = true
end
total_queue_count = total_queue_count + #card_entries
if total_lease_count + total_queue_count > 10000 then
  return response('invalid', 'receipt_queue_domain_exceeded', revision, incarnation)
end
local transition_raw = redis.call('GET', KEYS[4])
local transition_mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
if transition_mirror == false or (transition_raw and transition_raw ~= transition_mirror)
   or (not transition_raw and transition_mirror ~= '') then
  return response('invalid', 'receipt_transition_invalid', revision, incarnation)
end
if transition_raw then
  local transition = decode(transition_raw)
  if not transition or not target.known[transition.backend_id]
     or type(transition.owner_id) ~= 'string' or transition.owner_id == ''
     or not integrity_valid_generation(transition.generation)
     or type(transition.operation) ~= 'string' or transition.operation == ''
     or type(transition.require_idle) ~= 'boolean'
     or (transition.eviction_branch ~= nil
       and transition.eviction_branch ~= 'cancel'
       and transition.eviction_branch ~= 'unload')
     or not integrity_valid_integer(
          transition.created_at_ms, 1, 9007199254740991)
     or not integrity_valid_integer(
          transition.expires_at_ms, 1, 9007199254740991) then
    return response('invalid', 'receipt_transition_invalid', revision, incarnation)
  end
end
return response(
  'verified', '', revision, incarnation, receipt.collection_fingerprint)
"""
_LEDGER_INTEGRITY_LUA = r"""
local function integrity_now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function integrity_decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function integrity_valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local function integrity_valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function integrity_valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return integrity_valid_generation(item.generation)
end
local function integrity_valid_revision(value)
  if type(value) ~= 'string'
     or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 16 then return true end
  if string.len(value) > 16 then return false end
  return value < '9007199254740991'
end
local function integrity_has_revision_headroom(value)
  return integrity_valid_revision(value)
    and tonumber(value) < 9007199252740991
end
local function integrity_allows_heartbeat(value)
  return integrity_valid_revision(value)
    and tonumber(value) < 9007199253990991
end
local integrity_valid_states = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
local integrity_counted_states = {
  unknown=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true
}
local integrity_valid_lease_states = {active=true, uncertain=true, stale=true}
local function integrity_valid_allocation(item, backend_id)
  local reservation_state = item
    and (item.state == 'reserving' or item.state == 'loading')
  local has_reservation = item
    and type(item.reservation_lease_id) == 'string'
    and item.reservation_lease_id ~= ''
    and type(item.reservation_owner_id) == 'string'
    and item.reservation_owner_id ~= ''
  local reservation_present = item
    and (item.reservation_lease_id ~= nil
      or item.reservation_owner_id ~= nil)
  return item and item.backend_id == backend_id
    and integrity_valid_states[item.state]
    and integrity_valid_integer(item.budget_mb, 1, 9007199254740991)
    and integrity_valid_allocation_generation(item)
    and integrity_valid_integer(item.eviction_priority, -9007199254740991, 9007199254740991)
    and type(item.evictable) == 'boolean'
    and integrity_valid_integer(item.max_concurrency, 1, 10000)
    and integrity_valid_integer(item.last_used_at_ms, 1, 9007199254740991)
    and integrity_valid_integer(item.not_evict_before_ms, 0, 9007199254740991)
    and (item.state ~= 'resident' or item.not_evict_before_ms > 0)
    and (not reservation_state or has_reservation)
    and (reservation_state or not reservation_present)
end
local function integrity_valid_lease(lease, lease_id, backend_id)
  return lease and lease.lease_id == lease_id and lease.backend_id == backend_id
    and type(lease.owner_id) == 'string' and lease.owner_id ~= ''
    and type(lease.operation) == 'string' and lease.operation ~= ''
    and integrity_valid_generation(lease.generation)
    and integrity_valid_lease_states[lease.state]
    and integrity_valid_integer(lease.created_at_ms, 1, 9007199254740991)
    and integrity_valid_integer(lease.heartbeat_deadline_ms, 1, 9007199254740991)
    and integrity_valid_integer(lease.hard_deadline_ms, 1, 9007199254740991)
    and lease.created_at_ms <= lease.heartbeat_deadline_ms
    and lease.heartbeat_deadline_ms <= lease.hard_deadline_ms
end
local function integrity_table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function integrity_fault(resource_id, reason, now)
  if redis.call('HGET', KEYS[1], 'resource_id') == resource_id then
    if redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
      return
    end
    local revision = redis.call('HGET', KEYS[1], 'ledger_revision')
    if integrity_valid_revision(revision) then
      redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
    end
    redis.call('HSET', KEYS[1],
      'bootstrap_state', 'not_ready',
      'reconcile_deadline_ms', '0',
      'not_ready_reason', reason,
      'updated_at_ms', tostring(now))
  end
end
local function integrity_valid_ticket(
    ticket, expected_kind, expected_backend_id, known_backends)
  return ticket
    and integrity_valid_integer(ticket.expires_at_ms, 1, 9007199254740991)
    and integrity_valid_integer(ticket.enqueued_at_ms, 1, 9007199254740991)
    and ticket.enqueued_at_ms <= ticket.expires_at_ms
    and type(ticket.ticket_id) == 'string' and ticket.ticket_id ~= ''
    and type(ticket.owner_id) == 'string' and ticket.owner_id ~= ''
    and type(ticket.backend_id) == 'string' and ticket.backend_id ~= ''
    and integrity_valid_generation(ticket.membership_epoch)
    and ticket.kind == expected_kind
    and known_backends[ticket.backend_id]
    and (expected_backend_id == '' or ticket.backend_id == expected_backend_id)
end
local function inspect_membership_domains(
    resource_id, domain_raw, domain_fingerprint,
    membership_raw, membership_fingerprint,
    active_domain_raw, active_domain_fingerprint)
  local now = integrity_now_ms()
  if redis.call('HGET', KEYS[1], 'resource_id') == resource_id
     and redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
    return nil, 'not_ready', 'proof_reset_in_progress'
  end
  if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3' then
    integrity_fault(resource_id, 'membership_domain_invalid', now)
    return nil, 'ledger_corrupt', 'membership_domain_invalid'
  end
  local stored_domain_raw = redis.call('HGET', KEYS[1], 'backend_domain')
  local stored_membership_raw = redis.call('HGET', KEYS[1], 'membership_domain')
  local stored_active_domain_raw = redis.call('HGET', KEYS[1], 'active_backend_domain')
  if stored_domain_raw ~= domain_raw
     or stored_membership_raw ~= membership_raw
     or stored_active_domain_raw ~= active_domain_raw then
    return nil, 'not_ready', 'membership_domain_changed'
  end
  if redis.call('HGET', KEYS[1], 'backend_domain_fingerprint') ~= domain_fingerprint
     or redis.call('HGET', KEYS[1], 'membership_domain_fingerprint') ~= membership_fingerprint
     or redis.call('HGET', KEYS[1], 'active_backend_domain_fingerprint') ~= active_domain_fingerprint then
    integrity_fault(resource_id, 'membership_domain_invalid', now)
    return nil, 'ledger_corrupt', 'membership_domain_invalid'
  end
  local domain = integrity_decode(domain_raw)
  local membership_domain = integrity_decode(membership_raw)
  local active_domain = integrity_decode(active_domain_raw)
  if not domain or #domain > 64
     or not membership_domain or #membership_domain ~= #domain
     or not active_domain or #active_domain > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#membership_domain == 0 and membership_raw ~= '[]')
     or (#active_domain == 0 and active_domain_raw ~= '[]') then
    integrity_fault(resource_id, 'membership_domain_invalid', now)
    return nil, 'ledger_corrupt', 'membership_domain_invalid'
  end
  local known_backends = {}
  local memberships = {}
  local active_backends = {}
  local valid_membership_states = {pending=true, active=true, retiring=true}
  local active_index = 1
  local previous_backend_id = nil
  for index, backend_id in ipairs(domain) do
    local member = membership_domain[index]
    if type(backend_id) ~= 'string' or backend_id == ''
       or string.len(backend_id) > 128
       or (previous_backend_id and backend_id <= previous_backend_id)
       or known_backends[backend_id]
       or type(member) ~= 'table' or integrity_table_size(member) ~= 3
       or member.backend_id ~= backend_id
       or not integrity_valid_generation(member.membership_epoch)
       or not valid_membership_states[member.state] then
      integrity_fault(resource_id, 'membership_domain_invalid', now)
      return nil, 'ledger_corrupt', 'membership_domain_invalid'
    end
    known_backends[backend_id] = true
    previous_backend_id = backend_id
    memberships[backend_id] = member
    if member.state == 'active' then
      if active_domain[active_index] ~= backend_id then
        integrity_fault(resource_id, 'active_membership_domain_mismatch', now)
        return nil, 'ledger_corrupt', 'active_membership_domain_mismatch'
      end
      active_backends[backend_id] = true
      active_index = active_index + 1
    end
  end
  if active_index ~= #active_domain + 1 then
    integrity_fault(resource_id, 'active_membership_domain_mismatch', now)
    return nil, 'ledger_corrupt', 'active_membership_domain_mismatch'
  end
  return {
    domain=domain, known_backends=known_backends,
    memberships=memberships, active_backends=active_backends
  }, nil, nil
end
local function inspect_ledger(
    resource_id, domain_raw, domain_fingerprint,
    membership_raw, membership_fingerprint,
    active_domain_raw, active_domain_fingerprint,
    expected_incarnation, require_ready, focus_backend_id)
  local now = integrity_now_ms()
  if redis.call('HGET', KEYS[1], 'resource_id') ~= resource_id then
    return nil, 'not_ready', 'card_missing_or_mismatched'
  end
  if redis.call('HGET', KEYS[1], 'proof_reset_state') == 'prepared' then
    return nil, 'not_ready', 'proof_reset_in_progress'
  end
  local ledger_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation')
  if type(ledger_incarnation) ~= 'string' or ledger_incarnation == ''
     or string.len(ledger_incarnation) > 128 then
    integrity_fault(resource_id, 'ledger_incarnation_invalid', now)
    return nil, 'ledger_corrupt', 'ledger_incarnation_invalid'
  end
  if ledger_incarnation ~= expected_incarnation then
    return nil, 'not_ready', 'ledger_incarnation_changed'
  end
  local domains, domain_status, domain_reason = inspect_membership_domains(
    resource_id, domain_raw, domain_fingerprint,
    membership_raw, membership_fingerprint,
    active_domain_raw, active_domain_fingerprint)
  if not domains then return nil, domain_status, domain_reason end
  local domain = domains.domain
  if #KEYS ~= 4 + (#domain * 2) then
    integrity_fault(resource_id, 'backend_domain_invalid', now)
    return nil, 'ledger_corrupt', 'backend_domain_invalid'
  end
  local known_backends = domains.known_backends
  local lease_keys = {}
  local queue_keys = {}
  for index, backend_id in ipairs(domain) do
    lease_keys[backend_id] = KEYS[3 + (index * 2)]
    queue_keys[backend_id] = KEYS[4 + (index * 2)]
  end

  local allocatable = tonumber(redis.call('HGET', KEYS[1], 'allocatable_mb') or '-1')
  local ledger_revision = redis.call('HGET', KEYS[1], 'ledger_revision')
  local cached_committed = tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '-1')
  local cached_allocation_count = tonumber(redis.call('HGET', KEYS[1], 'allocation_count') or '-1')
  local deadline = tonumber(redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '-1')
  if redis.call('HGET', KEYS[1], 'ledger_version') ~= '3'
     or not integrity_valid_revision(ledger_revision)
     or not integrity_valid_integer(allocatable, 1, 9007199254740991)
     or not integrity_valid_integer(cached_committed, 0, 9007199254740991)
     or not integrity_valid_integer(cached_allocation_count, 0, 9007199254740991)
     or not integrity_valid_integer(deadline, 0, 9007199254740991) then
    integrity_fault(resource_id, 'card_schema_invalid', now)
    return nil, 'ledger_corrupt', 'card_schema_invalid'
  end
  if deadline > now + 300000 then
    integrity_fault(resource_id, 'reconcile_deadline_invalid', now)
    return nil, 'ledger_corrupt', 'reconcile_deadline_invalid'
  end
  if require_ready and (
      redis.call('HGET', KEYS[1], 'bootstrap_state') ~= 'ready'
      or deadline < 1 or deadline <= now) then
    return nil, 'not_ready', 'reconcile_expired'
  end

  local allocations = {}
  local committed = 0
  local allocation_count = 0
  if redis.call('HLEN', KEYS[2]) > 64 then
    integrity_fault(resource_id, 'allocation_domain_exceeded', now)
    return nil, 'ledger_corrupt', 'allocation_domain_exceeded'
  end
  local allocation_entries = redis.call('HGETALL', KEYS[2])
  for index = 1, #allocation_entries, 2 do
    local backend_id = allocation_entries[index]
    local allocation = integrity_decode(allocation_entries[index + 1])
    if not known_backends[backend_id]
       or not integrity_valid_allocation(allocation, backend_id) then
      integrity_fault(resource_id, 'allocation_invalid', now)
      return nil, 'ledger_corrupt', 'allocation_invalid'
    end
    allocations[backend_id] = allocation
    allocation_count = allocation_count + 1
    if integrity_counted_states[allocation.state] then
      committed = committed + allocation.budget_mb
    end
  end
  if allocation_count ~= cached_allocation_count
     or committed ~= cached_committed then
    integrity_fault(resource_id, 'allocation_cache_drift', now)
    return nil, 'ledger_corrupt', 'allocation_cache_drift'
  end

  local cached_lease_counts = integrity_decode(
    redis.call('HGET', KEYS[1], 'lease_counts'))
  if not cached_lease_counts
     or integrity_table_size(cached_lease_counts) ~= #domain then
    integrity_fault(resource_id, 'lease_count_cache_invalid', now)
    return nil, 'ledger_corrupt', 'lease_count_cache_invalid'
  end
  local leases = {}
  for _, backend_id in ipairs(domain) do
    local backend_leases = {}
    local lease_count = redis.call('HLEN', lease_keys[backend_id])
    if lease_count > 10000
       or (allocations[backend_id]
         and lease_count > allocations[backend_id].max_concurrency) then
      integrity_fault(resource_id, 'lease_domain_exceeded', now)
      return nil, 'ledger_corrupt', 'lease_domain_exceeded'
    end
    if lease_count > 0 and allocations[backend_id]
       and allocations[backend_id].generation == cjson.null then
      integrity_fault(resource_id, 'null_generation_has_leases', now)
      return nil, 'ledger_corrupt', 'null_generation_has_leases'
    end
    if backend_id == focus_backend_id then
      local lease_entries = redis.call('HGETALL', lease_keys[backend_id])
      for index = 1, #lease_entries, 2 do
        local lease = integrity_decode(lease_entries[index + 1])
        if not integrity_valid_lease(lease, lease_entries[index], backend_id) then
          integrity_fault(resource_id, 'lease_invalid', now)
          return nil, 'ledger_corrupt', 'lease_invalid'
        end
        backend_leases[lease_entries[index]] = lease
      end
    end
    if cached_lease_counts[backend_id] ~= lease_count
       or (lease_count > 0 and not allocations[backend_id]) then
      integrity_fault(resource_id, 'lease_count_cache_drift', now)
      return nil, 'ledger_corrupt', 'lease_count_cache_drift'
    end
    leases[backend_id] = backend_leases
  end
  for backend_id, allocation in pairs(allocations) do
    if allocation.state == 'reserving' or allocation.state == 'loading' then
      local reservation = leases[backend_id][allocation.reservation_lease_id]
        or integrity_decode(redis.call(
          'HGET', lease_keys[backend_id], allocation.reservation_lease_id))
      if not reservation
         or reservation.owner_id ~= allocation.reservation_owner_id
         or reservation.generation ~= allocation.generation then
        integrity_fault(resource_id, 'reservation_lease_mismatch', now)
        return nil, 'ledger_corrupt', 'reservation_lease_mismatch'
      end
    end
  end

  local cached_card_queue_count = tonumber(
    redis.call('HGET', KEYS[1], 'card_queue_count') or '-1')
  local card_queue_count = redis.call('LLEN', KEYS[3])
  local cached_backend_queue_counts = integrity_decode(
    redis.call('HGET', KEYS[1], 'backend_queue_counts'))
  if not integrity_valid_integer(card_queue_count, 0, 10000)
     or cached_card_queue_count ~= card_queue_count
     or not cached_backend_queue_counts
     or integrity_table_size(cached_backend_queue_counts) ~= #domain then
    integrity_fault(resource_id, 'queue_count_cache_drift', now)
    return nil, 'ledger_corrupt', 'queue_count_cache_drift'
  end
  local total_queue_count = card_queue_count
  for _, backend_id in ipairs(domain) do
    local queue_count = redis.call('LLEN', queue_keys[backend_id])
    if not integrity_valid_integer(queue_count, 0, 10000)
       or cached_backend_queue_counts[backend_id] ~= queue_count then
      integrity_fault(resource_id, 'queue_count_cache_drift', now)
      return nil, 'ledger_corrupt', 'queue_count_cache_drift'
    end
    total_queue_count = total_queue_count + queue_count
  end
  if total_queue_count > 10000 then
    integrity_fault(resource_id, 'queue_domain_exceeded', now)
    return nil, 'ledger_corrupt', 'queue_domain_exceeded'
  end

  local transition_raw = redis.call('GET', KEYS[4])
  local transition_mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
  local transition = nil
  if transition_mirror == false then
    integrity_fault(resource_id, 'transition_mirror_missing', now)
    return nil, 'ledger_corrupt', 'transition_mirror_missing'
  end
  if transition_raw then
    transition = integrity_decode(transition_raw)
    if transition_raw ~= transition_mirror or not transition
       or transition.resource_id ~= resource_id
       or not known_backends[transition.backend_id]
       or type(transition.owner_id) ~= 'string' or transition.owner_id == ''
       or not integrity_valid_generation(transition.generation)
       or type(transition.operation) ~= 'string' or transition.operation == ''
       or type(transition.require_idle) ~= 'boolean'
       or (transition.eviction_branch ~= nil
         and transition.eviction_branch ~= 'cancel'
         and transition.eviction_branch ~= 'unload')
       or not integrity_valid_integer(transition.created_at_ms, 1, 9007199254740991)
       or not integrity_valid_integer(transition.expires_at_ms, 1, 9007199254740991)
       or (transition.hard_deadline_ms ~= nil
         and (not integrity_valid_integer(
           transition.hard_deadline_ms, 1, 9007199254740991)
           or transition.expires_at_ms > transition.hard_deadline_ms)) then
      integrity_fault(resource_id, 'transition_mirror_mismatch', now)
      return nil, 'ledger_corrupt', 'transition_mirror_mismatch'
    end
    local transition_now = integrity_now_ms()
    if transition.eviction_branch == nil
       and transition.expires_at_ms <= transition_now then
      transition = nil
    end
  elseif transition_mirror ~= '' then
    local mirrored = integrity_decode(transition_mirror)
    if not mirrored
       or not integrity_valid_integer(mirrored.expires_at_ms, 1, 9007199254740991)
       or (mirrored.eviction_branch ~= nil
         and mirrored.eviction_branch ~= 'cancel'
         and mirrored.eviction_branch ~= 'unload')
       or (mirrored.hard_deadline_ms ~= nil
         and (not integrity_valid_integer(
           mirrored.hard_deadline_ms, 1, 9007199254740991)
           or mirrored.expires_at_ms > mirrored.hard_deadline_ms)) then
      integrity_fault(resource_id, 'transition_mirror_invalid', now)
      return nil, 'ledger_corrupt', 'transition_mirror_invalid'
    end
    if mirrored.eviction_branch ~= nil
       or mirrored.expires_at_ms > integrity_now_ms() then
      integrity_fault(resource_id, 'transition_key_missing_before_expiry', now)
      return nil, 'ledger_corrupt', 'transition_key_missing_before_expiry'
    end
  end
  return {
    now=now, domain=domain, known_backends=known_backends,
    memberships=domains.memberships, active_backends=domains.active_backends,
    lease_keys=lease_keys, queue_keys=queue_keys,
    allocations=allocations, leases=leases,
    lease_counts=cached_lease_counts,
    backend_queue_counts=cached_backend_queue_counts,
    transition=transition, committed=committed,
    ledger_incarnation=ledger_incarnation,
    ledger_revision=tonumber(ledger_revision),
    total_queue_count=total_queue_count,
    allocation_count=allocation_count, allocatable=allocatable
  }, nil, nil
end
"""
_COLLECT_RETIRED_BACKEND_LUA = _LEDGER_INTEGRITY_LUA + _COLLECT_RETIRED_BACKEND_LUA
_VERIFY_TOMBSTONE_GC_LUA = _LEDGER_INTEGRITY_LUA + _VERIFY_TOMBSTONE_GC_LUA
_ADMIT_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end

local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end

local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end

local function generation_greater(left, right)
  if not valid_generation(left) or not valid_generation(right) then return false end
  if string.len(left) ~= string.len(right) then
    return string.len(left) > string.len(right)
  end
  return left > right
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_generation(item.generation)
end
local function valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local valid_lease_states = {active=true, uncertain=true, stale=true}
local function valid_lease(lease, lease_id, backend_id)
  return lease and lease.lease_id == lease_id and lease.backend_id == backend_id
    and type(lease.owner_id) == 'string' and lease.owner_id ~= ''
    and type(lease.operation) == 'string' and lease.operation ~= ''
    and valid_generation(lease.generation) and valid_lease_states[lease.state]
    and valid_integer(lease.created_at_ms, 1, 9007199254740991)
    and valid_integer(lease.heartbeat_deadline_ms, 1, 9007199254740991)
    and valid_integer(lease.hard_deadline_ms, 1, 9007199254740991)
    and lease.created_at_ms <= lease.heartbeat_deadline_ms
    and lease.heartbeat_deadline_ms <= lease.hard_deadline_ms
end

local counted = {
  unknown=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true
}
local valid = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
local function calculate_committed(key)
  local committed = 0
  local entries = redis.call('HGETALL', key)
  for i = 1, #entries, 2 do
    local allocation = decode(entries[i + 1])
    local reservation_state = allocation
      and (allocation.state == 'reserving' or allocation.state == 'loading')
    local has_reservation = allocation
      and type(allocation.reservation_lease_id) == 'string'
      and allocation.reservation_lease_id ~= ''
      and type(allocation.reservation_owner_id) == 'string'
      and allocation.reservation_owner_id ~= ''
    local reservation_present = allocation
      and (allocation.reservation_lease_id ~= nil
        or allocation.reservation_owner_id ~= nil)
    if not allocation or not valid[allocation.state]
       or allocation.backend_id ~= entries[i]
       or not valid_integer(allocation.budget_mb, 1, 9007199254740991)
       or not valid_allocation_generation(allocation)
       or not valid_integer(allocation.eviction_priority, -9007199254740991, 9007199254740991)
       or type(allocation.evictable) ~= 'boolean'
       or not valid_integer(allocation.max_concurrency, 1, 10000)
       or not valid_integer(allocation.last_used_at_ms, 1, 9007199254740991)
       or not valid_integer(allocation.not_evict_before_ms, 0, 9007199254740991)
       or (allocation.state == 'resident' and allocation.not_evict_before_ms == 0)
       or (reservation_state and not has_reservation)
       or (not reservation_state and reservation_present) then
      return nil
    end
    if counted[allocation.state] then
      committed = committed + tonumber(allocation.budget_mb)
    end
  end
  return committed
end

local function read_queue(
    key, now, expected_kind, expected_backend_id, known_backends)
  local entries = redis.call('LRANGE', key, 0, -1)
  local live = {}
  local seen = {}
  for _, raw in ipairs(entries) do
    local ticket = decode(raw)
    if not ticket or not valid_integer(ticket.expires_at_ms, 1, 9007199254740991)
       or not valid_integer(ticket.enqueued_at_ms, 1, 9007199254740991)
       or ticket.enqueued_at_ms > ticket.expires_at_ms
       or type(ticket.ticket_id) ~= 'string' or ticket.ticket_id == ''
       or type(ticket.owner_id) ~= 'string' or ticket.owner_id == ''
       or type(ticket.backend_id) ~= 'string' or ticket.backend_id == ''
       or not valid_generation(ticket.membership_epoch)
       or ticket.kind ~= expected_kind
       or not known_backends[ticket.backend_id]
       or (expected_backend_id ~= '' and ticket.backend_id ~= expected_backend_id)
       or seen[ticket.ticket_id] then
      return nil, nil, 'corrupt'
    end
    seen[ticket.ticket_id] = true
    if ticket.expires_at_ms > now then
      table.insert(live, {raw=raw, ticket=ticket})
    end
  end
  local head = nil
  if #live > 0 then head = live[1].ticket end
  return head, live, nil
end
local function rewrite_queue(key, live, consumed_ticket_id)
  redis.call('DEL', key)
  for _, item in ipairs(live) do
    if item.ticket.ticket_id ~= consumed_ticket_id then
      redis.call('RPUSH', key, item.raw)
    end
  end
end

local ledger, integrity_status, integrity_reason = inspect_ledger(
  ARGV[1], ARGV[15], ARGV[16], ARGV[18], ARGV[19],
  ARGV[20], ARGV[21], ARGV[17], true, ARGV[2])
if not ledger then
  return cjson.encode({
    status=integrity_status, reason=integrity_reason,
    committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0,
    lease_count=0
  })
end
if not ledger.known_backends[ARGV[2]] then
  return cjson.encode({status='config_mismatch', reason='backend_outside_domain', committed_mb=ledger.committed, lease_count=0})
end
if not ledger.active_backends[ARGV[2]] then
  return cjson.encode({status='config_mismatch', reason='backend_not_active', committed_mb=ledger.committed, lease_count=0})
end
if ledger.memberships[ARGV[2]].membership_epoch ~= ARGV[22] then
  return cjson.encode({status='config_mismatch', reason='membership_epoch_changed', committed_mb=ledger.committed, lease_count=0})
end
if ARGV[23] ~= '0' and ARGV[23] ~= '1' then
  return cjson.encode({status='config_mismatch', reason='admission_mode_invalid', committed_mb=ledger.committed, lease_count=0})
end
if ARGV[24] ~= '0' and ARGV[24] ~= '1' then
  return cjson.encode({status='config_mismatch', reason='cold_owner_mode_invalid', committed_mb=ledger.committed, lease_count=0})
end
local reconcile_deadline_grace = tonumber(ARGV[25])
if not valid_integer(reconcile_deadline_grace, 0, 300000) then
  return cjson.encode({status='config_mismatch', reason='reconcile_deadline_grace_invalid', committed_mb=ledger.committed, lease_count=0})
end
if ARGV[23] == '1' and ARGV[24] == '1' then
  return cjson.encode({status='config_mismatch', reason='admission_mode_conflict', committed_mb=ledger.committed, lease_count=0})
end
local lease_key = ledger.lease_keys[ARGV[2]]
local backend_queue_key = ledger.queue_keys[ARGV[2]]

local resource_id = redis.call('HGET', KEYS[1], 'resource_id')
local bootstrap_state = redis.call('HGET', KEYS[1], 'bootstrap_state')
local allocatable = tonumber(redis.call('HGET', KEYS[1], 'allocatable_mb') or '-1')
if not resource_id or resource_id ~= ARGV[1] then
  return cjson.encode({status='not_ready', reason='card_missing_or_mismatched', committed_mb=0, lease_count=0})
end
if allocatable <= 0 then
  return cjson.encode({status='not_ready', reason='bootstrap_incomplete', committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0'), lease_count=0})
end

local now = now_ms()
local heartbeat_deadline = now + tonumber(ARGV[11])
local hard_deadline = now + tonumber(ARGV[12])
local renewed_reconcile_deadline = hard_deadline + reconcile_deadline_grace
if renewed_reconcile_deadline > now + 300000 then
  renewed_reconcile_deadline = now + 300000
end
local committed = calculate_committed(KEYS[2])
if not committed then
  return cjson.encode({status='ledger_corrupt', reason='allocation_decode_failed', committed_mb=0, lease_count=0})
end
if bootstrap_state ~= 'ready' then
  return cjson.encode({status='not_ready', reason='bootstrap_incomplete', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
end
local reconcile_deadline = tonumber(redis.call('HGET', KEYS[1], 'reconcile_deadline_ms') or '-1')
if not valid_integer(reconcile_deadline, 1, 9007199254740991)
   or reconcile_deadline <= now then
  return cjson.encode({status='not_ready', reason='reconcile_expired', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
end
if committed > allocatable then
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], 'bootstrap_state', 'not_ready', 'committed_mb', tostring(committed), 'updated_at_ms', tostring(now))
  return cjson.encode({status='not_ready', reason='committed_exceeds_allocatable', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
end
local transition = ledger.transition
local cold_owner_matches = ARGV[24] == '1'
  and transition
  and transition.backend_id == ARGV[2]
  and transition.owner_id == ARGV[9]
  and transition.generation == ARGV[4]
  and transition.operation == 'cold_admit'
  and transition.require_idle == true
if transition and transition.backend_id == ARGV[2]
   and not cold_owner_matches then
  return cjson.encode({status='transition_in_progress', reason='transition_owner_active', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
end
local lease_count = 0
local lease_entries = redis.call('HGETALL', lease_key)
local decoded_leases = {}
local existing_lease = nil
for i = 1, #lease_entries, 2 do
  local lease = decode(lease_entries[i + 1])
  if not valid_lease(lease, lease_entries[i], ARGV[2]) then
    return cjson.encode({status='ledger_corrupt', reason='lease_decode_failed', committed_mb=0, lease_count=lease_count})
  end
  table.insert(decoded_leases, {id=lease_entries[i], lease=lease})
  lease_count = lease_count + 1
  if lease_entries[i] == ARGV[8] then existing_lease = lease end
end
if existing_lease then
  if existing_lease.owner_id == ARGV[9] and existing_lease.generation == ARGV[4] and existing_lease.backend_id == ARGV[2] then
    local allocation = decode(redis.call('HGET', KEYS[2], ARGV[2]))
    if not allocation then
      return cjson.encode({status='ledger_corrupt', reason='idempotent_allocation_missing', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if allocation.state == 'reserving' or allocation.state == 'loading' then
      local reservation = decode(redis.call(
        'HGET', lease_key, allocation.reservation_lease_id))
      if not valid_lease(
          reservation, allocation.reservation_lease_id, allocation.backend_id)
         or reservation.owner_id ~= allocation.reservation_owner_id
         or reservation.generation ~= allocation.generation then
        return cjson.encode({status='ledger_corrupt', reason='reservation_lease_mismatch', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
      end
    end
    if allocation.generation ~= ARGV[4] then
      return cjson.encode({status='stale_generation', reason='idempotent_allocation_generation_changed', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if tonumber(allocation.budget_mb) ~= tonumber(ARGV[3])
       or tonumber(allocation.eviction_priority) ~= tonumber(ARGV[5])
       or tonumber(allocation.max_concurrency) ~= tonumber(ARGV[7]) then
      return cjson.encode({status='lease_conflict', reason='idempotent_config_mismatch', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if allocation.state ~= 'reserving' and allocation.state ~= 'loading' and allocation.state ~= 'resident' then
      return cjson.encode({status='lease_conflict', reason='idempotent_allocation_not_admissible', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if existing_lease.operation ~= ARGV[10] then
      return cjson.encode({status='lease_conflict', reason='idempotent_operation_mismatch', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if existing_lease.state ~= 'active' or tonumber(existing_lease.heartbeat_deadline_ms) <= now or tonumber(existing_lease.hard_deadline_ms) <= now then
      existing_lease.state = 'stale'
      redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
      redis.call('HSET', lease_key, ARGV[8], cjson.encode(existing_lease))
      return cjson.encode({status='lease_conflict', reason='idempotent_lease_not_active', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    if ARGV[23] == '1' and allocation.state ~= 'resident' then
      return cjson.encode({status='not_ready', reason='resident_allocation_required', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
    end
    return cjson.encode({
      status='admitted', reason='idempotent_lease', idempotent=true,
      committed_mb=committed,
      lease_count=redis.call('HLEN', lease_key),
      allocation_state=allocation and allocation.state or cjson.null,
      heartbeat_deadline_ms=existing_lease.heartbeat_deadline_ms,
      hard_deadline_ms=existing_lease.hard_deadline_ms
    })
  end
  return cjson.encode({status='lease_conflict', reason='lease_id_collision', committed_mb=committed, lease_count=redis.call('HLEN', lease_key)})
end

if not integrity_has_revision_headroom(
    redis.call('HGET', KEYS[1], 'ledger_revision')) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_rebase_required')
  return cjson.encode({
    status='not_ready', reason='ledger_revision_rebase_required',
    committed_mb=ledger.committed, lease_count=lease_count
  })
end

local allocation = decode(redis.call('HGET', KEYS[2], ARGV[2]))
local allocation_was_missing = allocation == nil
local increment = 0
local concurrency_limit = tonumber(ARGV[7])
if allocation then
  if allocation.state == 'reserving' or allocation.state == 'loading' then
    local reservation = decode(redis.call(
      'HGET', lease_key, allocation.reservation_lease_id))
    if not valid_lease(
        reservation, allocation.reservation_lease_id, allocation.backend_id)
       or reservation.owner_id ~= allocation.reservation_owner_id
       or reservation.generation ~= allocation.generation then
      return cjson.encode({status='ledger_corrupt', reason='reservation_lease_mismatch', committed_mb=committed, lease_count=lease_count})
    end
  end
  if allocation.state == 'unknown' then
    return cjson.encode({status='not_ready', reason='allocation_unknown', committed_mb=committed, lease_count=lease_count})
  end
  if tonumber(allocation.budget_mb) ~= tonumber(ARGV[3])
     or tonumber(allocation.eviction_priority) ~= tonumber(ARGV[5])
     or tonumber(allocation.max_concurrency) ~= tonumber(ARGV[7]) then
    return cjson.encode({status='config_mismatch', reason='allocation_config_mismatch', committed_mb=committed, lease_count=lease_count})
  end
  concurrency_limit = tonumber(allocation.max_concurrency)
  if allocation.state == 'draining' or allocation.state == 'unloading' then
    return cjson.encode({status='transition_in_progress', reason='allocation_' .. allocation.state, committed_mb=committed, lease_count=lease_count})
  end
  if allocation.state == 'reserving' or allocation.state == 'loading' or allocation.state == 'resident' then
    if allocation.generation ~= ARGV[4] then
      return cjson.encode({status='stale_generation', reason='allocation_generation_mismatch', committed_mb=committed, lease_count=lease_count})
    end
    if ARGV[23] == '0'
       and (allocation.state == 'reserving' or allocation.state == 'loading') then
      return cjson.encode({status='not_ready', reason='cold_allocation_in_progress', committed_mb=committed, lease_count=lease_count})
    end
  elseif allocation.state == 'unloaded' or allocation.state == 'cpu_fallback' then
    if not generation_greater(ARGV[4], allocation.generation) then
      return cjson.encode({status='stale_generation', reason='new_allocation_generation_not_monotonic', committed_mb=committed, lease_count=lease_count})
    end
    increment = tonumber(ARGV[3])
  else
    return cjson.encode({status='ledger_corrupt', reason='allocation_state_invalid', committed_mb=committed, lease_count=lease_count})
  end
else
  increment = tonumber(ARGV[3])
end
if increment > 0 and transition and transition.operation == 'cold_admit'
   and not cold_owner_matches then
  return cjson.encode({status='transition_in_progress', reason='cold_admission_owner_active', committed_mb=committed, lease_count=lease_count})
end
if ARGV[24] == '1' and not cold_owner_matches then
  return cjson.encode({status='transition_in_progress', reason='cold_admission_owner_required', committed_mb=committed, lease_count=lease_count})
end
if ARGV[24] == '1' and increment <= 0 then
  return cjson.encode({status='not_ready', reason='cold_allocation_required', committed_mb=committed, lease_count=lease_count})
end
if increment > 0 and ARGV[6] ~= '0' then
  return cjson.encode({status='config_mismatch', reason='cold_allocation_must_start_non_evictable', committed_mb=committed, lease_count=lease_count})
end
if ARGV[23] == '1' and (not allocation or allocation.state ~= 'resident') then
  return cjson.encode({status='not_ready', reason='resident_allocation_required', committed_mb=committed, lease_count=lease_count})
end

local backend_head, backend_live, backend_queue_error = read_queue(
  backend_queue_key, now, 'backend', ARGV[2], ledger.known_backends)
if backend_queue_error then
  return cjson.encode({status='ledger_corrupt', reason='backend_queue_decode_failed', committed_mb=committed, lease_count=lease_count})
end
if (ARGV[13] ~= '' and (
    not backend_head or backend_head.ticket_id ~= ARGV[13]
    or backend_head.owner_id ~= ARGV[9] or backend_head.backend_id ~= ARGV[2]
    or backend_head.membership_epoch ~= ARGV[22]))
   or (ARGV[13] == '' and backend_head) then
  return cjson.encode({status='concurrency_queued', reason='backend_fifo_wait', committed_mb=committed, lease_count=lease_count})
end
if lease_count >= concurrency_limit then
  return cjson.encode({status='concurrency_saturated', reason='max_concurrency_reached', committed_mb=committed, lease_count=lease_count})
end

local card_live = {}
local consumed_card_ticket = ''
if increment > 0 then
  local card_head, card_queue_error
  card_head, card_live, card_queue_error = read_queue(
    KEYS[3], now, 'card', '', ledger.known_backends)
  if card_queue_error then
    return cjson.encode({status='ledger_corrupt', reason='card_queue_decode_failed', committed_mb=committed, lease_count=lease_count})
  end
  if (ARGV[14] ~= '' and (
      not card_head or card_head.ticket_id ~= ARGV[14]
      or card_head.owner_id ~= ARGV[9] or card_head.backend_id ~= ARGV[2]
      or card_head.membership_epoch ~= ARGV[22]))
     or (ARGV[14] == '' and card_head) then
    return cjson.encode({status='card_queued', reason='card_fifo_wait', committed_mb=committed, lease_count=lease_count})
  end
  if committed + increment > allocatable then
    return cjson.encode({status='capacity_unavailable', reason='insufficient_capacity', committed_mb=committed, lease_count=lease_count})
  end
  allocation = {
    backend_id=ARGV[2], state='reserving', budget_mb=tonumber(ARGV[3]),
    generation=ARGV[4], eviction_priority=tonumber(ARGV[5]),
    evictable=false, max_concurrency=tonumber(ARGV[7]),
    reservation_lease_id=ARGV[8], reservation_owner_id=ARGV[9],
    last_used_at_ms=now, not_evict_before_ms=0
  }
  committed = committed + increment
  if card_head and card_head.ticket_id == ARGV[14] then
    consumed_card_ticket = ARGV[14]
  end
else
  allocation.last_used_at_ms = now
end

redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
for _, item in ipairs(decoded_leases) do
  local lease = item.lease
  if lease.state == 'active' and tonumber(lease.heartbeat_deadline_ms) <= now then
    lease.state = 'stale'
    redis.call('HSET', lease_key, item.id, cjson.encode(lease))
  elseif lease.state == 'uncertain' and tonumber(lease.hard_deadline_ms) <= now then
    lease.state = 'stale'
    redis.call('HSET', lease_key, item.id, cjson.encode(lease))
  end
end
redis.call('HSET', KEYS[2], ARGV[2], cjson.encode(allocation))

local lease = {
  lease_id=ARGV[8], backend_id=ARGV[2], owner_id=ARGV[9], generation=ARGV[4],
  operation=ARGV[10], state='active', created_at_ms=now,
  heartbeat_deadline_ms=heartbeat_deadline, hard_deadline_ms=hard_deadline
}
redis.call('HSET', lease_key, ARGV[8], cjson.encode(lease))
if increment > 0 then
  rewrite_queue(KEYS[3], card_live, consumed_card_ticket)
end
local consumed_backend_ticket = ''
if backend_head and backend_head.ticket_id == ARGV[13] then
  consumed_backend_ticket = ARGV[13]
end
rewrite_queue(backend_queue_key, backend_live, consumed_backend_ticket)
ledger.lease_counts[ARGV[2]] = ledger.lease_counts[ARGV[2]] + 1
ledger.backend_queue_counts[ARGV[2]] = #backend_live
if consumed_backend_ticket ~= '' then
  ledger.backend_queue_counts[ARGV[2]] = ledger.backend_queue_counts[ARGV[2]] - 1
end
local allocation_count = ledger.allocation_count
if allocation_was_missing then allocation_count = allocation_count + 1 end
redis.call('HSET', KEYS[1],
  'committed_mb', tostring(committed),
  'allocation_count', tostring(allocation_count),
  'lease_counts', cjson.encode(ledger.lease_counts),
  'backend_queue_counts', cjson.encode(ledger.backend_queue_counts),
  'updated_at_ms', tostring(now))
if renewed_reconcile_deadline > reconcile_deadline then
  redis.call('HSET', KEYS[1],
    'reconcile_deadline_ms', tostring(renewed_reconcile_deadline))
end
if ARGV[24] == '1' then
  redis.call('DEL', KEYS[4])
  redis.call('HSET', KEYS[1], 'transition_mirror', '')
end
if increment > 0 then
  local card_queue_count = #card_live
  if consumed_card_ticket ~= '' then card_queue_count = card_queue_count - 1 end
  redis.call('HSET', KEYS[1], 'card_queue_count', tostring(card_queue_count))
end
return cjson.encode({
  status='admitted', reason=(increment > 0 and 'allocation_reserved' or 'allocation_reused'),
  idempotent=false, committed_mb=committed, lease_count=lease_count + 1,
  allocation_state=allocation.state,
  heartbeat_deadline_ms=heartbeat_deadline, hard_deadline_ms=hard_deadline
})
"""
)
_MEMBERSHIP_DOMAIN_GUARD_LUA = r"""
local function guard_decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function guard_valid_epoch(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function guard_table_size(value)
  local count = 0
  for _, _ in pairs(value) do count = count + 1 end
  return count
end
local function guard_membership_domain(
    card_key, resource_id, backend_id, expected_incarnation,
    domain_raw, domain_fingerprint, membership_raw, membership_fingerprint,
    active_raw, active_fingerprint)
  if redis.call('HGET', card_key, 'resource_id') ~= resource_id
     or redis.call('HGET', card_key, 'proof_reset_state') == 'prepared'
     or redis.call('HGET', card_key, 'ledger_version') ~= '3'
     or redis.call('HGET', card_key, 'ledger_incarnation') ~= expected_incarnation
     or redis.call('HGET', card_key, 'backend_domain') ~= domain_raw
     or redis.call('HGET', card_key, 'backend_domain_fingerprint') ~= domain_fingerprint
     or redis.call('HGET', card_key, 'membership_domain') ~= membership_raw
     or redis.call('HGET', card_key, 'membership_domain_fingerprint') ~= membership_fingerprint
     or redis.call('HGET', card_key, 'active_backend_domain') ~= active_raw
     or redis.call('HGET', card_key, 'active_backend_domain_fingerprint') ~= active_fingerprint then
    return false
  end
  local domain = guard_decode(domain_raw)
  local memberships = guard_decode(membership_raw)
  local active = guard_decode(active_raw)
  if not domain or not memberships or not active
     or #domain > 64 or #memberships ~= #domain or #active > #domain
     or (#domain == 0 and domain_raw ~= '[]')
     or (#memberships == 0 and membership_raw ~= '[]')
     or (#active == 0 and active_raw ~= '[]') then
    return false
  end
  local valid_states = {pending=true, active=true, retiring=true}
  local previous = nil
  local active_index = 1
  local target_found = false
  for index, member_backend_id in ipairs(domain) do
    local member = memberships[index]
    if type(member_backend_id) ~= 'string' or member_backend_id == ''
       or string.len(member_backend_id) > 128
       or (previous and member_backend_id <= previous)
       or type(member) ~= 'table' or guard_table_size(member) ~= 3
       or member.backend_id ~= member_backend_id
       or not guard_valid_epoch(member.membership_epoch)
       or not valid_states[member.state] then
      return false
    end
    previous = member_backend_id
    if member_backend_id == backend_id then target_found = true end
    if member.state == 'active' then
      if active[active_index] ~= member_backend_id then return false end
      active_index = active_index + 1
    end
  end
  return target_found and active_index == #active + 1
end
"""
_LEASE_LUA = (
    _MEMBERSHIP_DOMAIN_GUARD_LUA
    + r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_generation(item.generation)
end
local function valid_integer(value)
  return type(value) == 'number' and value == math.floor(value)
    and value >= 1 and value <= 9007199254740991
end
local function valid_revision(value)
  if type(value) ~= 'string'
     or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 16 then return true end
  if string.len(value) > 16 then return false end
  return value < '9007199254740991'
end

if redis.call('HGET', KEYS[2], 'resource_id') == ARGV[6]
   and redis.call('HGET', KEYS[2], 'proof_reset_state') == 'prepared' then
  return cjson.encode({status='not_ready', reason='proof_reset_in_progress'})
end
if not guard_membership_domain(
    KEYS[2], ARGV[6], ARGV[7], ARGV[10],
    ARGV[8], ARGV[9], ARGV[11], ARGV[12], ARGV[13], ARGV[14]) then
  return cjson.encode({status='not_ready'})
end
local ledger_revision = redis.call('HGET', KEYS[2], 'ledger_revision')
if not valid_revision(ledger_revision) then
  redis.call('HSET', KEYS[2],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_invalid')
  return cjson.encode({status='not_ready'})
end
if tonumber(ledger_revision) >= 9007199253990991
   and ARGV[1] ~= 'release' then
  redis.call('HSET', KEYS[2],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_rebase_required')
  return cjson.encode({status='not_ready'})
end

local raw = redis.call('HGET', KEYS[1], ARGV[2])
if not raw then return cjson.encode({status='missing'}) end
local lease = decode(raw)
local valid_states = {active=true, uncertain=true, stale=true}
if not lease or lease.lease_id ~= ARGV[2]
   or lease.backend_id ~= ARGV[7]
   or type(lease.owner_id) ~= 'string' or lease.owner_id == ''
   or type(lease.operation) ~= 'string' or lease.operation == ''
   or not valid_generation(lease.generation) or not valid_states[lease.state]
   or not valid_integer(lease.created_at_ms)
   or not valid_integer(lease.heartbeat_deadline_ms)
   or not valid_integer(lease.hard_deadline_ms)
   or lease.created_at_ms > lease.heartbeat_deadline_ms
   or lease.heartbeat_deadline_ms > lease.hard_deadline_ms then
  return redis.error_reply('gpu arbiter lease decode failed')
end
if lease.owner_id ~= ARGV[3] then
  return cjson.encode({status='owner_mismatch', lease_state=lease.state, hard_deadline_ms=lease.hard_deadline_ms})
end
if lease.generation ~= ARGV[4] then
  return cjson.encode({status='stale_generation', lease_state=lease.state, hard_deadline_ms=lease.hard_deadline_ms})
end
local allocation_raw = redis.call('HGET', KEYS[3], ARGV[7])
local allocation = decode(allocation_raw)
local valid_allocation_states = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
if not allocation or allocation.backend_id ~= ARGV[7]
   or not valid_allocation_states[allocation.state]
   or not valid_allocation_generation(allocation) then
  return redis.error_reply('gpu arbiter allocation decode failed')
end
if ARGV[1] ~= 'release'
   and (type(allocation.not_evict_before_ms) ~= 'number'
     or allocation.not_evict_before_ms ~= math.floor(allocation.not_evict_before_ms)
     or allocation.not_evict_before_ms < 0
     or allocation.not_evict_before_ms > 9007199254740991) then
  return redis.error_reply('gpu arbiter allocation decode failed')
end
if allocation.generation == cjson.null and ARGV[1] ~= 'release' then
  return cjson.encode({status='not_ready', lease_state=lease.state, hard_deadline_ms=lease.hard_deadline_ms})
end

local lease_counts = decode(redis.call('HGET', KEYS[2], 'lease_counts'))
local actual_lease_count = redis.call('HLEN', KEYS[1])
local lease_count_mismatch = not lease_counts
  or type(lease_counts[ARGV[7]]) ~= 'number'
  or lease_counts[ARGV[7]] ~= actual_lease_count
if lease_count_mismatch then
  redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
  redis.call('HSET', KEYS[2],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'lease_count_cache_drift')
  return cjson.encode({status='not_ready'})
end

local now = now_ms()
if ARGV[1] == 'heartbeat' then
  if lease.state ~= 'active' or now >= tonumber(lease.heartbeat_deadline_ms) or now >= tonumber(lease.hard_deadline_ms) then
    if lease.state ~= 'stale' then
      lease.state = 'stale'
      redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
      redis.call('HSET', KEYS[1], ARGV[2], cjson.encode(lease))
    end
    return cjson.encode({status='stale', lease_state='stale', heartbeat_deadline_ms=lease.heartbeat_deadline_ms, hard_deadline_ms=lease.hard_deadline_ms})
  end
  local next_deadline = now + tonumber(ARGV[5])
  if next_deadline > tonumber(lease.hard_deadline_ms) then next_deadline = tonumber(lease.hard_deadline_ms) end
  lease.heartbeat_deadline_ms = next_deadline
  redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], ARGV[2], cjson.encode(lease))
  return cjson.encode({status='heartbeated', lease_state='active', heartbeat_deadline_ms=next_deadline, hard_deadline_ms=lease.hard_deadline_ms})
elseif ARGV[1] == 'uncertain' then
  if lease.state == 'stale' then
    return cjson.encode({status='stale', lease_state='stale', heartbeat_deadline_ms=lease.heartbeat_deadline_ms, hard_deadline_ms=lease.hard_deadline_ms})
  end
  if lease.state == 'active' then
    lease.state = 'uncertain'
    redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
    redis.call('HSET', KEYS[1], ARGV[2], cjson.encode(lease))
  end
  return cjson.encode({status='uncertain', lease_state=lease.state, heartbeat_deadline_ms=lease.heartbeat_deadline_ms, hard_deadline_ms=lease.hard_deadline_ms})
elseif ARGV[1] == 'release' then
  if (allocation.state == 'reserving' or allocation.state == 'loading')
     and allocation.reservation_lease_id == ARGV[2] then
    return cjson.encode({status='reservation_active', lease_state=lease.state, heartbeat_deadline_ms=lease.heartbeat_deadline_ms, hard_deadline_ms=lease.hard_deadline_ms})
  end
  redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
  redis.call('HDEL', KEYS[1], ARGV[2])
  if lease_counts and type(lease_counts[ARGV[7]]) == 'number' then
    lease_counts[ARGV[7]] = actual_lease_count - 1
    redis.call('HSET', KEYS[2], 'lease_counts', cjson.encode(lease_counts))
  end
  return cjson.encode({status='released'})
end
return redis.error_reply('unsupported gpu arbiter lease operation')
"""
)
_SWEEP_LEASES_LUA = (
    _MEMBERSHIP_DOMAIN_GUARD_LUA
    + r"""
local function valid_revision(value)
  if type(value) ~= 'string'
     or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 16 then return true end
  if string.len(value) > 16 then return false end
  return value < '9007199254740991'
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function valid_integer(value)
  return type(value) == 'number' and value == math.floor(value)
    and value >= 1 and value <= 9007199254740991
end

if redis.call('HGET', KEYS[2], 'resource_id') == ARGV[1]
   and redis.call('HGET', KEYS[2], 'proof_reset_state') == 'prepared' then
  return cjson.encode({
    status='not_ready', reason='proof_reset_in_progress', changed=0, total=0
  })
end
if not guard_membership_domain(
    KEYS[2], ARGV[1], ARGV[2], ARGV[5],
    ARGV[3], ARGV[4], ARGV[6], ARGV[7], ARGV[8], ARGV[9]) then
  return cjson.encode({status='not_ready', changed=0, total=0})
end
if not valid_revision(redis.call('HGET', KEYS[2], 'ledger_revision')) then
  redis.call('HSET', KEYS[2],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_invalid')
  return cjson.encode({status='not_ready', changed=0, total=0})
end
local t = redis.call('TIME')
local now = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local entries = redis.call('HGETALL', KEYS[1])
local changed = 0
local total = 0
local decoded = {}
for i = 1, #entries, 2 do
  local ok, lease = pcall(cjson.decode, entries[i + 1])
  local valid_states = {active=true, uncertain=true, stale=true}
  if not ok or type(lease) ~= 'table'
     or lease.lease_id ~= entries[i] or lease.backend_id ~= ARGV[2]
     or type(lease.owner_id) ~= 'string' or lease.owner_id == ''
     or type(lease.operation) ~= 'string' or lease.operation == ''
     or not valid_generation(lease.generation) or not valid_states[lease.state]
     or not valid_integer(lease.created_at_ms)
     or not valid_integer(lease.heartbeat_deadline_ms)
     or not valid_integer(lease.hard_deadline_ms)
     or lease.created_at_ms > lease.heartbeat_deadline_ms
     or lease.heartbeat_deadline_ms > lease.hard_deadline_ms then
    return redis.error_reply('gpu arbiter lease decode failed')
  end
  table.insert(decoded, {id=entries[i], lease=lease, changed=false})
end
for _, item in ipairs(decoded) do
  local lease = item.lease
  total = total + 1
  if lease.state == 'active' and tonumber(lease.heartbeat_deadline_ms) <= now then
    lease.state = 'stale'
    item.changed = true
    changed = changed + 1
  elseif lease.state == 'uncertain' and tonumber(lease.hard_deadline_ms) <= now then
    lease.state = 'stale'
    item.changed = true
    changed = changed + 1
  end
end
if changed > 0 then
  redis.call('HINCRBY', KEYS[2], 'ledger_revision', 1)
  for _, item in ipairs(decoded) do
    if item.changed then
      redis.call('HSET', KEYS[1], item.id, cjson.encode(item.lease))
    end
  end
end
return cjson.encode({status='swept', changed=changed, total=total})
"""
)
_QUEUE_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local now = integrity_now_ms()
if redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[8] then
  return cjson.encode({status='not_ready', ticket_id=ARGV[2]})
end
local queue_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation')
if type(queue_incarnation) ~= 'string' or queue_incarnation == ''
   or string.len(queue_incarnation) > 128 then
  integrity_fault(ARGV[8], 'ledger_incarnation_invalid', now)
  return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
end
if queue_incarnation ~= ARGV[11] then
  return cjson.encode({status='not_ready', ticket_id=ARGV[2]})
end
if not integrity_valid_revision(
    redis.call('HGET', KEYS[1], 'ledger_revision')) then
  integrity_fault(ARGV[8], 'ledger_revision_invalid', now)
  return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
end
local revision_bumped = false

local domains, domain_status = inspect_membership_domains(
  ARGV[8], ARGV[9], ARGV[10], ARGV[12], ARGV[13], ARGV[14], ARGV[15])
if not domains then
  return cjson.encode({status=domain_status, ticket_id=ARGV[2]})
end
local domain = domains.domain
local queue_key = nil
local backend_queue_counts = integrity_decode(
  redis.call('HGET', KEYS[1], 'backend_queue_counts'))
if not backend_queue_counts or #KEYS ~= 4 + (#domain * 2) then
  integrity_fault(ARGV[8], 'backend_domain_invalid', now)
  return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
end
local known_backends = domains.known_backends
for index, backend_id in ipairs(domain) do
  if ARGV[5] == 'backend' and backend_id == ARGV[3] then
    queue_key = KEYS[4 + (index * 2)]
  end
end
if not known_backends[ARGV[3]] then
  return cjson.encode({status='not_ready', ticket_id=ARGV[2]})
end
if ARGV[1] == 'enqueue' and not domains.active_backends[ARGV[3]] then
  return cjson.encode({status='config_mismatch', ticket_id=ARGV[2]})
end
if ARGV[1] == 'enqueue'
   and domains.memberships[ARGV[3]].membership_epoch ~= ARGV[16] then
  return cjson.encode({status='config_mismatch', ticket_id=ARGV[2]})
end
if ARGV[5] == 'card' then queue_key = KEYS[3] end
if not queue_key then
  integrity_fault(ARGV[8], 'backend_domain_invalid', now)
  return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
end

local inspected_ledger = nil
if ARGV[1] == 'enqueue' then
  local ledger, integrity_status = inspect_ledger(
    ARGV[8], ARGV[9], ARGV[10], ARGV[12], ARGV[13],
    ARGV[14], ARGV[15], ARGV[11], true, ARGV[3])
  if not ledger then
    return cjson.encode({status=integrity_status, ticket_id=ARGV[2]})
  end
  local allocation = ledger.allocations[ARGV[3]]
  if allocation and allocation.state == 'unknown'
     and allocation.generation == cjson.null then
    return cjson.encode({status='not_ready', ticket_id=ARGV[2]})
  end
  inspected_ledger = ledger
else
  local cached_count = ARGV[5] == 'card'
    and tonumber(redis.call('HGET', KEYS[1], 'card_queue_count') or '-1')
    or backend_queue_counts[ARGV[3]]
  if cached_count ~= redis.call('LLEN', queue_key) then
    integrity_fault(ARGV[8], 'queue_count_cache_drift', now)
    return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
  end
end

local function persist_queue_count(count)
  if not revision_bumped then
    redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
    revision_bumped = true
  end
  if ARGV[5] == 'card' then
    redis.call('HSET', KEYS[1], 'card_queue_count', tostring(count))
  else
    backend_queue_counts[ARGV[3]] = count
    redis.call('HSET', KEYS[1],
      'backend_queue_counts', cjson.encode(backend_queue_counts))
  end
end

local entries = redis.call('LRANGE', queue_key, 0, -1)
local live = {}
local existing_position = nil
local seen = {}
for _, raw in ipairs(entries) do
  local ticket = integrity_decode(raw)
  if not integrity_valid_ticket(ticket, ARGV[5],
      ARGV[5] == 'backend' and ARGV[3] or '', known_backends)
     or seen[ticket.ticket_id] then
    integrity_fault(ARGV[8], 'queue_invalid', now)
    return cjson.encode({status='ledger_corrupt', ticket_id=ARGV[2]})
  end
  seen[ticket.ticket_id] = true
  if ticket.expires_at_ms > now then
    table.insert(live, raw)
    if ticket.ticket_id == ARGV[2] then existing_position = #live end
  end
end
local pruned = #live ~= #entries
local function rewrite_live(excluded_ticket_id)
  redis.call('DEL', queue_key)
  local remaining = 0
  for _, raw in ipairs(live) do
    local ticket = integrity_decode(raw)
    if ticket.ticket_id ~= excluded_ticket_id then
      redis.call('RPUSH', queue_key, raw)
      remaining = remaining + 1
    end
  end
  persist_queue_count(remaining)
  return remaining
end

if ARGV[1] == 'enqueue' then
  if existing_position then
    local ticket = cjson.decode(live[existing_position])
    if ticket.owner_id ~= ARGV[4] or ticket.backend_id ~= ARGV[3]
       or ticket.kind ~= ARGV[5]
       or ticket.membership_epoch ~= ARGV[16] then
      return cjson.encode({status='ticket_conflict', ticket_id=ARGV[2]})
    end
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='queued', ticket_id=ARGV[2], position=existing_position, expires_at_ms=ticket.expires_at_ms})
  end
  if not integrity_has_revision_headroom(
      redis.call('HGET', KEYS[1], 'ledger_revision')) then
    redis.call('HSET', KEYS[1],
      'bootstrap_state', 'not_ready',
      'reconcile_deadline_ms', '0',
      'not_ready_reason', 'ledger_revision_rebase_required')
    return cjson.encode({status='not_ready', ticket_id=ARGV[2]})
  end
  local effective_total_queue_count = inspected_ledger.total_queue_count
    - #entries + #live
  if #live >= tonumber(ARGV[7]) or effective_total_queue_count >= 10000 then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='full', ticket_id=ARGV[2]})
  end
  local ticket = {
    ticket_id=ARGV[2], backend_id=ARGV[3], owner_id=ARGV[4], kind=ARGV[5],
    membership_epoch=ARGV[16],
    enqueued_at_ms=now, expires_at_ms=now + tonumber(ARGV[6])
  }
  if pruned then rewrite_live(nil) end
  redis.call('RPUSH', queue_key, cjson.encode(ticket))
  persist_queue_count(#live + 1)
  return cjson.encode({status='queued', ticket_id=ARGV[2], position=#live + 1, expires_at_ms=ticket.expires_at_ms})
elseif ARGV[1] == 'cancel' then
  if not existing_position then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='missing', ticket_id=ARGV[2]})
  end
  local existing_ticket = cjson.decode(live[existing_position])
  if existing_ticket.backend_id ~= ARGV[3]
     or existing_ticket.kind ~= ARGV[5] then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='ticket_conflict', ticket_id=ARGV[2], position=existing_position, expires_at_ms=existing_ticket.expires_at_ms})
  end
  if existing_ticket.owner_id ~= ARGV[4] then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='owner_mismatch', ticket_id=ARGV[2], position=existing_position, expires_at_ms=existing_ticket.expires_at_ms})
  end
  rewrite_live(ARGV[2])
  return cjson.encode({status='cancelled', ticket_id=ARGV[2]})
elseif ARGV[1] == 'position' then
  if not existing_position then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='missing', ticket_id=ARGV[2]})
  end
  local ticket = cjson.decode(live[existing_position])
  if ticket.backend_id ~= ARGV[3] or ticket.kind ~= ARGV[5] then
    if pruned then rewrite_live(nil) end
    return cjson.encode({status='ticket_conflict', ticket_id=ARGV[2], position=existing_position, expires_at_ms=ticket.expires_at_ms})
  end
  if pruned then rewrite_live(nil) end
  return cjson.encode({status='queued', ticket_id=ARGV[2], position=existing_position, expires_at_ms=ticket.expires_at_ms})
end
return redis.error_reply('unsupported gpu arbiter queue operation')
"""
)
_TRANSITION_OWNER_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function generation_greater(left, right)
  if not valid_generation(left) or not valid_generation(right) then return false end
  if string.len(left) ~= string.len(right) then
    return string.len(left) > string.len(right)
  end
  return left > right
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_generation(item.generation)
end

if redis.call('HGET', KEYS[1], 'resource_id') ~= ARGV[2] then
  return cjson.encode({status='not_ready'})
end
local now = now_ms()
local owner_incarnation = redis.call('HGET', KEYS[1], 'ledger_incarnation')
if type(owner_incarnation) ~= 'string' or owner_incarnation == ''
   or string.len(owner_incarnation) > 128 then
  integrity_fault(ARGV[2], 'ledger_incarnation_invalid', now)
  return cjson.encode({status='ledger_corrupt'})
end
if owner_incarnation ~= ARGV[11] then
  return cjson.encode({status='not_ready'})
end
if not integrity_valid_revision(
    redis.call('HGET', KEYS[1], 'ledger_revision')) then
  integrity_fault(ARGV[2], 'ledger_revision_invalid', now)
  return cjson.encode({status='ledger_corrupt'})
end
local owner_domains, owner_domain_status = inspect_membership_domains(
  ARGV[2], ARGV[9], ARGV[10], ARGV[12], ARGV[13], ARGV[14], ARGV[15])
if not owner_domains then
  return cjson.encode({status=owner_domain_status})
end
if not owner_domains.known_backends[ARGV[3]] then
  return cjson.encode({status='not_ready'})
end
local ledger = nil
local target_lease_key = nil
if ARGV[1] == 'acquire' or ARGV[1] == 'revalidate' then
  local integrity_status
  ledger, integrity_status = inspect_ledger(
    ARGV[2], ARGV[9], ARGV[10], ARGV[12], ARGV[13],
    ARGV[14], ARGV[15], ARGV[11], ARGV[6] == 'cold_admit', ARGV[3])
  if not ledger then
    return cjson.encode({status=integrity_status})
  end
  if ledger.memberships[ARGV[3]].membership_epoch ~= ARGV[16] then
    return cjson.encode({status='config_mismatch'})
  end
  if ARGV[6] == 'cold_admit' and not ledger.active_backends[ARGV[3]] then
    return cjson.encode({status='config_mismatch'})
  end
  target_lease_key = ledger.lease_keys[ARGV[3]]
end
if ARGV[1] == 'heartbeat'
   and not integrity_allows_heartbeat(
     redis.call('HGET', KEYS[1], 'ledger_revision')) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_rebase_required')
  return cjson.encode({status='not_ready'})
end

local raw = nil
local current = nil
if ARGV[1] == 'acquire' or ARGV[1] == 'revalidate' then
  current = ledger.transition
else
  raw = redis.call('GET', KEYS[4])
  current = decode(raw)
  if raw and not current then
    integrity_fault(ARGV[2], 'transition_invalid', now)
    return cjson.encode({status='ledger_corrupt'})
  end
  local mirror = redis.call('HGET', KEYS[1], 'transition_mirror')
  if mirror == false then
    integrity_fault(ARGV[2], 'transition_mirror_missing', now)
    return cjson.encode({status='ledger_corrupt'})
  end
  if raw and raw ~= mirror then
    integrity_fault(ARGV[2], 'transition_mirror_mismatch', now)
    return cjson.encode({status='ledger_corrupt'})
  elseif not raw and mirror and mirror ~= '' then
    local mirrored = decode(mirror)
    if not mirrored or type(mirrored.expires_at_ms) ~= 'number'
       or (mirrored.eviction_branch ~= nil
         and mirrored.eviction_branch ~= 'cancel'
         and mirrored.eviction_branch ~= 'unload') then
      integrity_fault(ARGV[2], 'transition_mirror_invalid', now)
      return cjson.encode({status='ledger_corrupt'})
    end
    if mirrored.eviction_branch ~= nil
       or mirrored.expires_at_ms > now_ms() then
      integrity_fault(ARGV[2], 'transition_key_missing_before_expiry', now)
      return cjson.encode({status='ledger_corrupt'})
    end
  end
end
if current and (
    current.resource_id ~= ARGV[2]
    or type(current.backend_id) ~= 'string' or current.backend_id == ''
    or type(current.owner_id) ~= 'string' or current.owner_id == ''
    or not valid_generation(current.generation)
    or type(current.operation) ~= 'string' or current.operation == ''
    or type(current.require_idle) ~= 'boolean'
    or (current.eviction_branch ~= nil
      and current.eviction_branch ~= 'cancel'
      and current.eviction_branch ~= 'unload')
    or type(current.created_at_ms) ~= 'number'
    or type(current.expires_at_ms) ~= 'number'
    or (current.hard_deadline_ms ~= nil
      and (type(current.hard_deadline_ms) ~= 'number'
        or current.expires_at_ms > current.hard_deadline_ms))) then
  integrity_fault(ARGV[2], 'transition_invalid', now)
  return cjson.encode({status='ledger_corrupt'})
end
now = now_ms()
if current and current.eviction_branch == nil
   and current.expires_at_ms <= now then
  current = nil
end
if current and current.eviction_branch ~= nil
   and current.hard_deadline_ms ~= nil
   and current.hard_deadline_ms <= now
   and ARGV[1] ~= 'release' then
  return cjson.encode({status='invalid_transition', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
end

local function validate_acquire_target()
  local allocation_raw = redis.call('HGET', KEYS[2], ARGV[3])
  if ARGV[6] == 'cold_admit' then
    if ARGV[8] ~= '1' then return 'invalid_transition', nil end
    if not allocation_raw then
      if redis.call('HLEN', target_lease_key) > 0 then
        return 'active_leases', nil
      end
      return nil, nil
    end
    local cold_allocation = decode(allocation_raw)
    if not cold_allocation or cold_allocation.backend_id ~= ARGV[3]
       or not valid_allocation_generation(cold_allocation)
       or type(cold_allocation.state) ~= 'string' then
      return 'ledger_corrupt', nil
    end
    if cold_allocation.state ~= 'unloaded'
       and cold_allocation.state ~= 'cpu_fallback' then
      return 'invalid_transition', cold_allocation.generation
    end
    if not generation_greater(ARGV[5], cold_allocation.generation) then
      return 'stale_generation', cold_allocation.generation
    end
    if redis.call('HLEN', target_lease_key) > 0 then
      return 'active_leases', cold_allocation.generation
    end
    return nil, cold_allocation.generation
  end
  if not allocation_raw then return 'missing', nil end
  local allocation = decode(allocation_raw)
  if not allocation or allocation.backend_id ~= ARGV[3]
     or not valid_allocation_generation(allocation)
     or type(allocation.state) ~= 'string' then
    return 'ledger_corrupt', nil
  end
  if ARGV[8] == '1' and allocation.state ~= 'resident' then
    return 'invalid_transition', allocation.generation
  end
  if allocation.state == 'resident' then
    if not generation_greater(ARGV[5], allocation.generation) then
      return 'stale_generation', allocation.generation
    end
  elseif allocation.state == 'draining' then
    if ARGV[5] ~= allocation.generation
       and not generation_greater(ARGV[5], allocation.generation) then
      return 'stale_generation', allocation.generation
    end
  elseif allocation.state == 'unloading' then
    if ARGV[5] ~= allocation.generation then
      return 'stale_generation', allocation.generation
    end
  else
    return 'invalid_transition', allocation.generation
  end
  if ARGV[8] == '1' and redis.call('HLEN', target_lease_key) > 0 then
    return 'active_leases', allocation.generation
  end
  return nil, allocation.generation
end

if ARGV[1] == 'acquire' then
  if current then
    if current.owner_id == ARGV[4] and current.backend_id == ARGV[3]
       and current.generation == ARGV[5] and current.operation == ARGV[6]
       and current.require_idle == (ARGV[8] == '1') then
      local target_error, target_generation = validate_acquire_target()
      if target_error then
        return cjson.encode({status=target_error, generation=target_generation})
      end
      if not integrity_has_revision_headroom(
          redis.call('HGET', KEYS[1], 'ledger_revision')) then
        return cjson.encode({status='acquired', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms, idempotent=true})
      end
      current.expires_at_ms = now + tonumber(ARGV[7])
      if current.hard_deadline_ms ~= nil
         and current.expires_at_ms > current.hard_deadline_ms then
        current.expires_at_ms = current.hard_deadline_ms
      end
      local encoded = cjson.encode(current)
      if current.eviction_branch ~= nil then
        redis.call('SET', KEYS[4], encoded)
      else
        redis.call('SET', KEYS[4], encoded, 'PXAT',
          tostring(current.expires_at_ms + 1000))
      end
      redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
      redis.call('HSET', KEYS[1], 'transition_mirror', encoded)
      return cjson.encode({status='acquired', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms, idempotent=true})
    end
    return cjson.encode({status='busy', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
  end
  if not integrity_has_revision_headroom(
      redis.call('HGET', KEYS[1], 'ledger_revision')) then
    redis.call('HSET', KEYS[1],
      'bootstrap_state', 'not_ready',
      'reconcile_deadline_ms', '0',
      'not_ready_reason', 'ledger_revision_rebase_required')
    return cjson.encode({status='not_ready'})
  end
  local target_error, target_generation = validate_acquire_target()
  if target_error then
    return cjson.encode({status=target_error, generation=target_generation})
  end
  local owner = {
    resource_id=ARGV[2], backend_id=ARGV[3], owner_id=ARGV[4],
    generation=ARGV[5], operation=ARGV[6],
    require_idle=(ARGV[8] == '1'),
    created_at_ms=now, expires_at_ms=now + tonumber(ARGV[7])
  }
  local encoded = cjson.encode(owner)
  redis.call('SET', KEYS[4], encoded, 'PXAT',
    tostring(owner.expires_at_ms + 1000))
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded)
  return cjson.encode({status='acquired', owner_id=owner.owner_id, generation=owner.generation, expires_at_ms=owner.expires_at_ms, idempotent=false})
end

if not current then return cjson.encode({status='missing'}) end
if current.owner_id ~= ARGV[4] then
  return cjson.encode({status='owner_mismatch', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
end
if current.generation ~= ARGV[5] then
  return cjson.encode({status='stale_generation', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
end
if current.operation ~= ARGV[6] or current.backend_id ~= ARGV[3] then
  return cjson.encode({status='operation_mismatch', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
end

if ARGV[1] == 'revalidate' then
  if current.require_idle ~= (ARGV[8] == '1') then
    return cjson.encode({status='operation_mismatch', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
  end
  local target_error, target_generation = validate_acquire_target()
  if target_error then
    return cjson.encode({status=target_error, generation=target_generation})
  end
  if not integrity_has_revision_headroom(
      redis.call('HGET', KEYS[1], 'ledger_revision')) then
    redis.call('HSET', KEYS[1],
      'bootstrap_state', 'not_ready',
      'reconcile_deadline_ms', '0',
      'not_ready_reason', 'ledger_revision_rebase_required')
    return cjson.encode({status='not_ready'})
  end
  current.expires_at_ms = now + tonumber(ARGV[7])
  if current.hard_deadline_ms ~= nil
     and current.expires_at_ms > current.hard_deadline_ms then
    current.expires_at_ms = current.hard_deadline_ms
  end
  local encoded = cjson.encode(current)
  if current.eviction_branch ~= nil then
    redis.call('SET', KEYS[4], encoded)
  else
    redis.call('SET', KEYS[4], encoded, 'PXAT',
      tostring(current.expires_at_ms + 1000))
  end
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded)
  return cjson.encode({status='renewed', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
elseif ARGV[1] == 'heartbeat' then
  local allocation = decode(redis.call('HGET', KEYS[2], ARGV[3]))
  if allocation and valid_allocation_generation(allocation)
     and allocation.generation == cjson.null then
    return cjson.encode({status='not_ready', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
  end
  current.expires_at_ms = now + tonumber(ARGV[7])
  if current.hard_deadline_ms ~= nil
     and current.expires_at_ms > current.hard_deadline_ms then
    current.expires_at_ms = current.hard_deadline_ms
  end
  local encoded = cjson.encode(current)
  if current.eviction_branch ~= nil then
    redis.call('SET', KEYS[4], encoded)
  else
    redis.call('SET', KEYS[4], encoded, 'PXAT',
      tostring(current.expires_at_ms + 1000))
  end
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded)
  return cjson.encode({status='renewed', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
elseif ARGV[1] == 'release' then
  if current.eviction_branch ~= nil then
    local allocation = decode(redis.call('HGET', KEYS[2], ARGV[3]))
    if not allocation or not valid_allocation_generation(allocation)
       or allocation.backend_id ~= ARGV[3] then
      return cjson.encode({status='ledger_corrupt', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
    end
    local terminal = (current.eviction_branch == 'cancel'
        and (allocation.state == 'resident' or allocation.state == 'unknown'))
      or (current.eviction_branch == 'unload'
        and (allocation.state == 'unloaded' or allocation.state == 'unknown'))
    if not terminal then
      return cjson.encode({status='invalid_transition', owner_id=current.owner_id, generation=current.generation, expires_at_ms=current.expires_at_ms})
    end
  end
  redis.call('DEL', KEYS[4])
  redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
  redis.call('HSET', KEYS[1], 'transition_mirror', '')
  return cjson.encode({status='released', owner_id=current.owner_id, generation=current.generation})
end
return redis.error_reply('unsupported gpu arbiter transition owner operation')
"""
)
_BEGIN_IDLE_EVICTION_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local function valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function generation_greater(left, right)
  if not valid_generation(left) or not valid_generation(right) then return false end
  if string.len(left) ~= string.len(right) then
    return string.len(left) > string.len(right)
  end
  return left > right
end
local function candidate_less(left, right)
  if left.eviction_priority ~= right.eviction_priority then
    return left.eviction_priority < right.eviction_priority
  end
  if left.last_used_at_ms ~= right.last_used_at_ms then
    return left.last_used_at_ms < right.last_used_at_ms
  end
  return left.backend_id < right.backend_id
end
local function response(
    status, reason, committed, shortfall, victim, owner, idempotent, retry_at)
  return cjson.encode({
    status=status, reason=reason, committed_mb=committed,
    shortfall_mb=shortfall, victim_backend_id=victim and victim.backend_id or nil,
    victim_generation=victim and victim.generation or nil,
    victim_budget_mb=victim and victim.budget_mb or nil,
    owner_id=owner and owner.owner_id or nil,
    owner_expires_at_ms=owner and owner.expires_at_ms or nil,
    owner_hard_deadline_ms=owner and owner.hard_deadline_ms or nil,
    retry_at_ms=retry_at,
    idempotent=idempotent or false
  })
end

local ledger, integrity_status = inspect_ledger(
  ARGV[1], ARGV[14], ARGV[15], ARGV[17], ARGV[18],
  ARGV[19], ARGV[20], ARGV[16], false, ARGV[2])
if not ledger then
  return response(integrity_status, integrity_status, 0, 0, nil, nil, false)
end
local committed = ledger.committed
local has_card_ticket = ARGV[21] ~= '' or ARGV[22] ~= ''
if ARGV[23] ~= '0' and ARGV[23] ~= '1' then
  return response('config_mismatch', 'allow_busy_invalid', committed,
    0, nil, nil, false)
end
local allow_busy = ARGV[23] == '1'
if has_card_ticket and (ARGV[21] == '' or ARGV[22] == '') then
  return response('config_mismatch', 'card_ticket_identity_incomplete', committed,
    0, nil, nil, false)
end
local requester_budget = tonumber(ARGV[4])
local requester_priority = tonumber(ARGV[5])
if not valid_integer(requester_budget, 1, 9007199254740991)
   or not valid_integer(requester_priority, -9007199254740991, 9007199254740991)
   or not valid_generation(ARGV[8]) or not valid_generation(ARGV[9])
   or not generation_greater(ARGV[9], ARGV[8])
   or ARGV[11] ~= 'evict'
   or not valid_integer(tonumber(ARGV[12]), 1, 2147483647)
   or not valid_integer(tonumber(ARGV[13]), tonumber(ARGV[12]), 2147483647) then
  return response('config_mismatch', 'eviction_request_invalid', committed, 0, nil, nil, false)
end
if not ledger.active_backends[ARGV[2]]
   or not ledger.active_backends[ARGV[6]]
   or ledger.memberships[ARGV[2]].membership_epoch ~= ARGV[3]
   or ledger.memberships[ARGV[6]].membership_epoch ~= ARGV[7]
   or ARGV[2] == ARGV[6] then
  return response('config_mismatch', 'eviction_membership_mismatch', committed, 0, nil, nil, false)
end

local transition = ledger.transition
if transition then
  local victim = ledger.allocations[ARGV[6]]
  local exact = transition.backend_id == ARGV[6]
    and transition.owner_id == ARGV[10]
    and transition.generation == ARGV[9]
    and transition.operation == ARGV[11]
    and transition.require_idle == (not allow_busy)
    and transition.requester_backend_id == ARGV[2]
    and transition.requester_membership_epoch == ARGV[3]
    and transition.requester_budget_mb == requester_budget
    and transition.requester_eviction_priority == requester_priority
    and transition.requester_card_ticket_id == ARGV[21]
    and transition.requester_queue_owner_id == ARGV[22]
    and transition.victim_membership_epoch == ARGV[7]
    and transition.victim_source_generation == ARGV[8]
    and victim and victim.state == 'draining'
    and victim.generation == ARGV[9]
    and victim.evictable == true
    and (allow_busy or ledger.lease_counts[ARGV[6]] == 0)
  if exact then
    local shortfall = math.max(0, requester_budget - (ledger.allocatable - committed))
    return response('selected', 'idempotent_idle_eviction', committed, shortfall,
      victim, transition, true)
  end
  return response('transition_in_progress', 'transition_owner_active', committed,
    0, nil, nil, false)
end

local card_head = nil
local seen_card_tickets = {}
local card_entries = redis.call('LRANGE', KEYS[3], 0, -1)
for _, raw in ipairs(card_entries) do
  local ticket = integrity_decode(raw)
  if not integrity_valid_ticket(ticket, 'card', '', ledger.known_backends)
     or seen_card_tickets[ticket.ticket_id] then
    integrity_fault(ARGV[1], 'card_queue_invalid', ledger.now)
    return response('ledger_corrupt', 'card_queue_invalid', committed,
      0, nil, nil, false)
  end
  seen_card_tickets[ticket.ticket_id] = true
  if not card_head and ticket.expires_at_ms > ledger.now then
    card_head = ticket
  end
end
if (has_card_ticket and (
    not card_head or card_head.ticket_id ~= ARGV[21]
    or card_head.backend_id ~= ARGV[2]
    or card_head.owner_id ~= ARGV[22]
    or card_head.membership_epoch ~= ARGV[3]))
   or (not has_card_ticket and card_head) then
  return response('card_queued', 'card_fifo_wait', committed,
    0, nil, nil, false)
end

local requester = ledger.allocations[ARGV[2]]
if requester then
  if requester.state ~= 'unloaded' and requester.state ~= 'cpu_fallback' then
    return response('stale_selection', 'requester_allocation_changed', committed,
      0, nil, nil, false)
  end
  if requester.budget_mb ~= requester_budget
     or requester.eviction_priority ~= requester_priority then
    return response('config_mismatch', 'requester_config_mismatch', committed,
      0, nil, nil, false)
  end
end
if ledger.lease_counts[ARGV[2]] ~= 0 then
  return response('stale_selection', 'requester_has_leases', committed,
    0, nil, nil, false)
end
if requester_budget > ledger.allocatable then
  return response('capacity_unavailable', 'request_exceeds_allocatable', committed,
    requester_budget - ledger.allocatable, nil, nil, false)
end
local shortfall = requester_budget - (ledger.allocatable - committed)
if shortfall <= 0 then
  return response('capacity_available', 'capacity_already_available', committed,
    0, nil, nil, false)
end

local eligible_ready = {}
local eligible_all = {}
local eligible_ready_capacity = 0
local eligible_total_capacity = 0
local possible_capacity = 0
for backend_id, allocation in pairs(ledger.allocations) do
  if backend_id ~= ARGV[2]
     and ledger.active_backends[backend_id]
     and allocation.state == 'resident'
     and allocation.evictable == true
     and allocation.eviction_priority <= requester_priority then
    possible_capacity = math.min(shortfall,
      possible_capacity + allocation.budget_mb)
    if allow_busy or ledger.lease_counts[backend_id] == 0 then
      table.insert(eligible_all, allocation)
      eligible_total_capacity = math.min(
        shortfall, eligible_total_capacity + allocation.budget_mb)
      if allocation.not_evict_before_ms <= ledger.now then
        table.insert(eligible_ready, allocation)
        eligible_ready_capacity = math.min(
          shortfall, eligible_ready_capacity + allocation.budget_mb)
      end
    end
  end
end
if eligible_ready_capacity < shortfall then
  if eligible_total_capacity >= shortfall then
    table.sort(eligible_all, function(left, right)
      if left.not_evict_before_ms ~= right.not_evict_before_ms then
        return left.not_evict_before_ms < right.not_evict_before_ms
      end
      return candidate_less(left, right)
    end)
    local available = 0
    local retry_at = ledger.now
    for _, allocation in ipairs(eligible_all) do
      available = math.min(shortfall, available + allocation.budget_mb)
      retry_at = math.max(retry_at, allocation.not_evict_before_ms)
      if available >= shortfall then break end
    end
    return response('cooldown_active', 'eligible_victim_cooldown_active', committed,
      shortfall, nil, nil, false, retry_at)
  end
  if not allow_busy and possible_capacity >= shortfall then
    return response('victim_busy', 'eligible_victim_has_leases', committed,
      shortfall, nil, nil, false)
  end
  return response('capacity_unavailable', 'eligible_capacity_insufficient', committed,
    shortfall, nil, nil, false)
end
table.sort(eligible_ready, candidate_less)
local victim = eligible_ready[1]
if not victim or victim.backend_id ~= ARGV[6]
   or victim.generation ~= ARGV[8] then
  return response('stale_selection', 'victim_order_or_generation_changed', committed,
    shortfall, victim, nil, false)
end
if not integrity_has_revision_headroom(
    redis.call('HGET', KEYS[1], 'ledger_revision')) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_rebase_required')
  return response('not_ready', 'ledger_revision_rebase_required', committed,
    shortfall, nil, nil, false)
end

local now = ledger.now
local hard_deadline = now + tonumber(ARGV[13])
local expires_at = math.min(now + tonumber(ARGV[12]), hard_deadline)
local owner = {
  resource_id=ARGV[1], backend_id=ARGV[6], owner_id=ARGV[10],
  generation=ARGV[9], operation=ARGV[11], require_idle=(not allow_busy),
  requester_backend_id=ARGV[2], requester_membership_epoch=ARGV[3],
  requester_budget_mb=requester_budget,
  requester_eviction_priority=requester_priority,
  requester_card_ticket_id=ARGV[21], requester_queue_owner_id=ARGV[22],
  victim_membership_epoch=ARGV[7], victim_source_generation=ARGV[8],
  created_at_ms=now, expires_at_ms=expires_at,
  hard_deadline_ms=hard_deadline
}
victim.state = 'draining'
victim.generation = ARGV[9]
victim.last_used_at_ms = now
victim.reservation_lease_id = nil
victim.reservation_owner_id = nil
local encoded_owner = cjson.encode(owner)
redis.call('SET', KEYS[4], encoded_owner, 'PXAT', tostring(expires_at + 1000))
redis.call('HSET', KEYS[1], 'transition_mirror', encoded_owner,
  'updated_at_ms', tostring(now))
redis.call('HSET', KEYS[2], ARGV[6], cjson.encode(victim))
redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
return response('selected', 'idle_victim_selected', committed, shortfall,
  victim, owner, false)
"""
)
_ARM_EVICTION_CANCEL_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local function response(status, branch, state, generation, idempotent)
  return cjson.encode({
    status=status, branch=branch or cjson.null, state=state or cjson.null,
    generation=generation or cjson.null, idempotent=idempotent or false
  })
end

local ledger, integrity_status = inspect_ledger(
  ARGV[1], ARGV[5], ARGV[6], ARGV[8], ARGV[9],
  ARGV[10], ARGV[11], ARGV[7], false, ARGV[2])
if not ledger then
  return response(integrity_status, nil, nil, nil, false)
end

local transition = ledger.transition
if not transition then
  return response('missing', nil, nil, nil, false)
end
if transition.owner_id ~= ARGV[4] then
  return response('owner_mismatch', transition.eviction_branch,
    nil, transition.generation, false)
end
if transition.generation ~= ARGV[3] then
  return response('stale_generation', transition.eviction_branch,
    nil, transition.generation, false)
end
if transition.resource_id ~= ARGV[1]
   or transition.backend_id ~= ARGV[2]
   or transition.operation ~= 'evict'
   or transition.require_idle ~= false then
  return response('invalid_transition', transition.eviction_branch,
    nil, transition.generation, false)
end
if transition.eviction_branch == 'unload' then
  return response('branch_conflict', 'unload', nil,
    transition.generation, false)
end

local allocation = ledger.allocations[ARGV[2]]
if not allocation then
  return response('missing', transition.eviction_branch, nil, nil, false)
end
if allocation.generation ~= ARGV[3] then
  return response('stale_generation', transition.eviction_branch,
    allocation.state, allocation.generation, false)
end
if allocation.state ~= 'draining' or allocation.evictable ~= true then
  return response('invalid_transition', transition.eviction_branch,
    allocation.state, allocation.generation, false)
end
if transition.eviction_branch == 'cancel' then
  return response('armed', 'cancel', allocation.state,
    allocation.generation, true)
end
if not integrity_has_revision_headroom(
    redis.call('HGET', KEYS[1], 'ledger_revision')) then
  redis.call('HSET', KEYS[1],
    'bootstrap_state', 'not_ready',
    'reconcile_deadline_ms', '0',
    'not_ready_reason', 'ledger_revision_rebase_required')
  return response('not_ready', nil, allocation.state,
    allocation.generation, false)
end

transition.eviction_branch = 'cancel'
local encoded = cjson.encode(transition)
redis.call('SET', KEYS[4], encoded)
redis.call('HSET', KEYS[1],
  'transition_mirror', encoded,
  'updated_at_ms', tostring(ledger.now))
redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
return response('armed', 'cancel', allocation.state,
  allocation.generation, false)
"""
)
_TRANSITION_LUA = (
    _LEDGER_INTEGRITY_LUA
    + r"""
local function now_ms()
  local t = redis.call('TIME')
  return (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
end
local function decode(raw)
  if not raw then return nil end
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function valid_generation(value)
  if type(value) ~= 'string' or not string.match(value, '^[1-9][0-9]*$') then
    return false
  end
  if string.len(value) < 19 then return true end
  if string.len(value) > 19 then return false end
  return value <= '9223372036854775807'
end
local function generation_greater(left, right)
  if not valid_generation(left) or not valid_generation(right) then return false end
  if string.len(left) ~= string.len(right) then
    return string.len(left) > string.len(right)
  end
  return left > right
end
local function valid_allocation_generation(item)
  if item.generation == cjson.null then
    return item.state == 'unknown' and item.evictable == false
  end
  return valid_generation(item.generation)
end
local function valid_integer(value, minimum, maximum)
  return type(value) == 'number' and value == math.floor(value)
    and value >= minimum and value <= maximum
end
local allowed = {
  reserving={loading=true, unknown=true, unloaded=true},
  loading={resident=true, unknown=true, unloaded=true, cpu_fallback=true},
  resident={draining=true},
  draining={resident=true, unknown=true, unloading=true},
  unloading={unloaded=true, unknown=true}
}
local counted = {
  unknown=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true
}
local valid = {
  unknown=true, unloaded=true, reserving=true, loading=true, resident=true,
  draining=true, unloading=true, cpu_fallback=true
}
if ARGV[17] ~= '0' and ARGV[17] ~= '1' then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
if ARGV[18] ~= '0' and ARGV[18] ~= '1' then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
if ARGV[19] ~= '0' and ARGV[19] ~= '1' then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
local resident_cooldown_ms = tonumber(ARGV[21])
if not valid_integer(resident_cooldown_ms, 0, 2147483647) then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
local cold_finalize = ARGV[17] == '1'
local eviction_transition = ARGV[19] == '1'
if ARGV[4] == 'resident' and not cold_finalize and not eviction_transition then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
if cold_finalize and (
    (ARGV[4] ~= 'resident' and ARGV[4] ~= 'unknown'
      and ARGV[4] ~= 'unloaded' and ARGV[4] ~= 'cpu_fallback')
    or (ARGV[4] == 'resident' and ARGV[18] ~= '1')
    or (ARGV[4] ~= 'resident' and ARGV[18] ~= '0')
    or (ARGV[4] == 'resident' and resident_cooldown_ms == 0)
    or (ARGV[4] ~= 'resident' and resident_cooldown_ms ~= 0)) then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
if not cold_finalize and resident_cooldown_ms ~= 0 then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
local eviction_allowed = {
  resident={draining=true},
  draining={resident=true, unloading=true, unknown=true},
  unloading={unloaded=true, unknown=true}
}
local function eviction_branch_matches(transition, source_state, target_state)
  if source_state == 'resident' and target_state == 'draining' then
    return transition.eviction_branch == nil
  end
  if source_state == 'draining' and target_state == 'unloading' then
    return transition.eviction_branch == 'unload'
  end
  if source_state == 'unloading' then
    return transition.eviction_branch == 'unload'
  end
  if transition.require_idle == true then
    return transition.eviction_branch == nil
  end
  if source_state == 'draining'
     and (target_state == 'resident' or target_state == 'unknown') then
    return transition.eviction_branch == 'cancel'
  end
  return false
end
local eviction_changes_generation =
  (ARGV[20] == 'resident' and ARGV[4] == 'draining')
  or (ARGV[20] == 'draining' and ARGV[4] == 'resident')
if eviction_transition and (
    cold_finalize or ARGV[20] == ''
    or not eviction_allowed[ARGV[20]]
    or not eviction_allowed[ARGV[20]][ARGV[4]]
    or ARGV[8] == '' or ARGV[9] ~= 'evict'
    or (eviction_changes_generation and ARGV[5] == '')
    or (not eviction_changes_generation and ARGV[5] ~= '')) then
  return cjson.encode({status='invalid_transition', committed_mb=0})
end
local function valid_allocation(item, backend_id)
  local reservation_state = item
    and (item.state == 'reserving' or item.state == 'loading')
  local has_reservation = item
    and type(item.reservation_lease_id) == 'string'
    and item.reservation_lease_id ~= ''
    and type(item.reservation_owner_id) == 'string'
    and item.reservation_owner_id ~= ''
  local reservation_present = item
    and (item.reservation_lease_id ~= nil or item.reservation_owner_id ~= nil)
  return item and item.backend_id == backend_id and valid[item.state]
    and valid_integer(item.budget_mb, 1, 9007199254740991)
    and valid_allocation_generation(item)
    and valid_integer(item.eviction_priority, -9007199254740991, 9007199254740991)
    and type(item.evictable) == 'boolean'
    and valid_integer(item.max_concurrency, 1, 10000)
    and valid_integer(item.last_used_at_ms, 1, 9007199254740991)
    and valid_integer(item.not_evict_before_ms, 0, 9007199254740991)
    and (item.state ~= 'resident' or item.not_evict_before_ms > 0)
    and (not reservation_state or has_reservation)
    and (reservation_state or not reservation_present)
end
local valid_lease_states = {active=true, uncertain=true, stale=true}
local function valid_lease(lease, lease_id, backend_id)
  return lease and lease.lease_id == lease_id and lease.backend_id == backend_id
    and type(lease.owner_id) == 'string' and lease.owner_id ~= ''
    and type(lease.operation) == 'string' and lease.operation ~= ''
    and valid_generation(lease.generation) and valid_lease_states[lease.state]
    and valid_integer(lease.created_at_ms, 1, 9007199254740991)
    and valid_integer(lease.heartbeat_deadline_ms, 1, 9007199254740991)
    and valid_integer(lease.hard_deadline_ms, 1, 9007199254740991)
    and lease.created_at_ms <= lease.heartbeat_deadline_ms
    and lease.heartbeat_deadline_ms <= lease.hard_deadline_ms
end

local ledger, integrity_status = inspect_ledger(
  ARGV[1], ARGV[10], ARGV[11], ARGV[13], ARGV[14],
  ARGV[15], ARGV[16], ARGV[12], false, ARGV[2])
if not ledger then
  return cjson.encode({
    status=integrity_status,
    committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0') or 0
  })
end
if not ledger.known_backends[ARGV[2]] then
  return cjson.encode({status='not_ready', committed_mb=ledger.committed})
end
local lease_key = ledger.lease_keys[ARGV[2]]

local card_resource_id = redis.call('HGET', KEYS[1], 'resource_id')
if not card_resource_id or card_resource_id ~= ARGV[1] then
  return cjson.encode({status='not_ready', committed_mb=0})
end

local raw = redis.call('HGET', KEYS[2], ARGV[2])
if not raw then
  return cjson.encode({status='missing', committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
local allocation = decode(raw)
if not valid_allocation(allocation, ARGV[2]) then
  return cjson.encode({status='ledger_corrupt', committed_mb=0})
end

local lease_count = 0
local lease_entries = redis.call('HGETALL', lease_key)
for i = 1, #lease_entries, 2 do
  local lease = decode(lease_entries[i + 1])
  if not valid_lease(lease, lease_entries[i], ARGV[2]) then
    return cjson.encode({status='ledger_corrupt', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
  end
  lease_count = lease_count + 1
end

if eviction_transition and allocation.state == ARGV[4] then
  local result_generation = ARGV[3]
  if ARGV[5] ~= '' then result_generation = ARGV[5] end
  local owner_generation = result_generation
  if ARGV[20] == 'draining' and ARGV[4] == 'resident' then
    owner_generation = ARGV[3]
  end
  local transition = ledger.transition
  local terminal = allocation.state == 'unloaded' or allocation.state == 'unknown'
  if allocation.generation ~= result_generation then
    return cjson.encode({status='stale_generation', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  if not transition or transition.resource_id ~= ARGV[1]
     or transition.backend_id ~= ARGV[2]
     or transition.owner_id ~= ARGV[8]
     or transition.operation ~= ARGV[9]
     or transition.generation ~= owner_generation then
    return cjson.encode({status='owner_mismatch', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  if not eviction_branch_matches(transition, ARGV[20], ARGV[4]) then
    return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  local requires_zero_leases = allocation.state == 'unloading'
    or allocation.state == 'unloaded'
  local requires_idle = ARGV[20] == 'resident' and ARGV[4] == 'draining'
    and transition.require_idle == true
  if ((requires_zero_leases or requires_idle) and lease_count > 0)
     or (terminal and allocation.evictable ~= false)
     or (not terminal and allocation.evictable ~= true) then
    if lease_count > 0 then
      return cjson.encode({status='active_leases', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
    end
    return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  return cjson.encode({status='transitioned', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed, idempotent=true})
end

if allocation.generation ~= ARGV[3] then
  return cjson.encode({status='stale_generation', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end

if cold_finalize and allocation.state == ARGV[4]
   and allocation.state ~= 'reserving' and allocation.state ~= 'loading' then
  local terminal_lease = decode(redis.call('HGET', lease_key, ARGV[6]))
  if ARGV[6] == '' or ARGV[7] == ''
     or (allocation.state ~= 'resident' and lease_count ~= 1)
     or not valid_lease(terminal_lease, ARGV[6], ARGV[2])
     or terminal_lease.owner_id ~= ARGV[7]
     or terminal_lease.generation ~= allocation.generation then
    return cjson.encode({status='owner_mismatch', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  if allocation.evictable ~= (ARGV[18] == '1') then
    return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  if (allocation.state == 'resident' and allocation.not_evict_before_ms <= 0)
     or (allocation.state ~= 'resident' and allocation.not_evict_before_ms ~= 0) then
    return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
  end
  return cjson.encode({status='transitioned', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed, idempotent=true})
end

if not allowed[allocation.state] or not allowed[allocation.state][ARGV[4]] then
  return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
if eviction_transition and (
    allocation.state ~= ARGV[20] or allocation.evictable ~= true) then
  return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
local changes_generation = (allocation.state == 'resident' and ARGV[4] == 'draining')
  or (allocation.state == 'draining' and ARGV[4] == 'resident')
if ARGV[5] ~= '' and not changes_generation then
  return cjson.encode({status='invalid_transition', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
if ARGV[5] ~= '' and not generation_greater(ARGV[5], allocation.generation) then
  return cjson.encode({status='stale_generation', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
if changes_generation and ARGV[5] == '' then
  return cjson.encode({status='stale_generation', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end

local workload_owned = allocation.state == 'reserving' or allocation.state == 'loading'
local owner_lease = nil
if workload_owned then
  if ARGV[6] == '' or ARGV[7] == ''
     or allocation.reservation_lease_id ~= ARGV[6]
     or allocation.reservation_owner_id ~= ARGV[7] then
    return cjson.encode({status='owner_mismatch', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
  end
  owner_lease = decode(redis.call('HGET', lease_key, ARGV[6]))
  if not valid_lease(owner_lease, ARGV[6], ARGV[2])
     or owner_lease.owner_id ~= ARGV[7]
     or owner_lease.generation ~= allocation.generation then
    return cjson.encode({status='owner_mismatch', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
  end
end

local transition_owned = allocation.state == 'resident'
  or allocation.state == 'draining' or allocation.state == 'unloading'
local transition_requires_idle = false
local transition_owner = nil
local arm_unload_branch = false
if transition_owned then
  local transition = ledger.transition
  local owner_generation = allocation.generation
  if ARGV[5] ~= ''
     and not (allocation.state == 'draining' and ARGV[4] == 'resident') then
    owner_generation = ARGV[5]
  end
  if not transition or ARGV[8] == '' or ARGV[9] == ''
     or transition.resource_id ~= ARGV[1]
     or transition.backend_id ~= ARGV[2]
     or transition.owner_id ~= ARGV[8]
     or transition.operation ~= ARGV[9]
     or transition.generation ~= owner_generation then
    return cjson.encode({status='owner_mismatch', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
  end
  transition_owner = transition
  transition_requires_idle = transition.require_idle == true
  if eviction_transition then
    if allocation.state == 'resident' and ARGV[4] == 'draining' then
      if transition.eviction_branch ~= nil then
        return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
      end
    elseif allocation.state == 'draining' and ARGV[4] == 'unloading' then
      if transition.eviction_branch == 'cancel' then
        return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
      end
      arm_unload_branch = transition.eviction_branch == nil
    elseif allocation.state == 'unloading' then
      if transition.eviction_branch ~= 'unload' then
        return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
      end
    elseif transition.require_idle == true then
      if transition.eviction_branch ~= nil then
        return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
      end
    elseif allocation.state == 'draining'
       and (ARGV[4] == 'resident' or ARGV[4] == 'unknown') then
      if transition.eviction_branch ~= 'cancel' then
        return cjson.encode({status='branch_conflict', state=allocation.state, generation=allocation.generation, committed_mb=ledger.committed})
      end
    end
  end
end

if lease_count > 0 and (
    (allocation.state == 'draining' and ARGV[4] == 'unloading')
    or (allocation.state == 'unloading' and ARGV[4] == 'unloaded')
    or (eviction_transition and allocation.state == 'resident'
      and ARGV[4] == 'draining' and transition_requires_idle)) then
  return cjson.encode({status='active_leases', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end
if workload_owned and (ARGV[4] == 'unloaded' or ARGV[4] == 'cpu_fallback')
   and lease_count ~= 1 then
  return cjson.encode({status='active_leases', state=allocation.state, generation=allocation.generation, committed_mb=tonumber(redis.call('HGET', KEYS[1], 'committed_mb') or '0')})
end

local committed = 0
local entries = redis.call('HGETALL', KEYS[2])
for i = 1, #entries, 2 do
  local decoded_ok, item = pcall(cjson.decode, entries[i + 1])
  if not decoded_ok or not valid_allocation(item, entries[i]) then
    return cjson.encode({status='ledger_corrupt', committed_mb=committed})
  end
  if counted[item.state] then committed = committed + tonumber(item.budget_mb) end
end
if arm_unload_branch then
  transition_owner.eviction_branch = 'unload'
  local encoded_transition = cjson.encode(transition_owner)
  redis.call('SET', KEYS[4], encoded_transition)
  redis.call('HSET', KEYS[1], 'transition_mirror', encoded_transition)
end
local previous_state = allocation.state
allocation.state = ARGV[4]
if counted[previous_state] and not counted[allocation.state] then
  committed = committed - tonumber(allocation.budget_mb)
elseif not counted[previous_state] and counted[allocation.state] then
  committed = committed + tonumber(allocation.budget_mb)
end
if ARGV[5] ~= '' then allocation.generation = ARGV[5] end
local transition_now = now_ms()
if cold_finalize then
  allocation.evictable = (ARGV[18] == '1')
  if allocation.state == 'resident' then
    if transition_now > 9007199254740991 - resident_cooldown_ms then
      return cjson.encode({status='invalid_transition', state=previous_state, generation=allocation.generation, committed_mb=ledger.committed})
    end
    allocation.not_evict_before_ms = transition_now + resident_cooldown_ms
  else
    allocation.not_evict_before_ms = 0
  end
end
if eviction_transition
   and (allocation.state == 'unloaded' or allocation.state == 'unknown') then
  allocation.evictable = false
  allocation.not_evict_before_ms = 0
end
if allocation.state ~= 'reserving' and allocation.state ~= 'loading' then
  allocation.reservation_lease_id = nil
  allocation.reservation_owner_id = nil
end
allocation.last_used_at_ms = transition_now
redis.call('HINCRBY', KEYS[1], 'ledger_revision', 1)
redis.call('HSET', KEYS[2], ARGV[2], cjson.encode(allocation))
redis.call('HSET', KEYS[1], 'committed_mb', tostring(committed), 'updated_at_ms', tostring(transition_now))
return cjson.encode({status='transitioned', state=allocation.state, generation=allocation.generation, committed_mb=committed, idempotent=false})
"""
)
