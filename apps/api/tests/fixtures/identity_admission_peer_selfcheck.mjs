import assert from "node:assert/strict";

import { parseIdentityReceipt } from "./identity_admission_peer.mjs";

const success = `{"schema":"aap-identity/v1","ok":true,"data":{"actor_id":"00000000-0000-0000-0000-000000000001","name":"Fixture","email":"fixture@example.test","role":"viewer","is_active":true,"credential_type":"jwt","credential_id":"00000000-0000-0000-0000-000000000002","generation":9223372036854775807,"scopes":null,"expires_at":"2030-01-02T03:04:05.123456+00:00"}}`;
const receipt = parseIdentityReceipt(success, 200);
assert.equal(receipt.ok, true);
assert.equal(receipt.principal.generation_decimal, "9223372036854775807");
assert.equal(receipt.principal.expires_at, "2030-01-02T03:04:05.123456+00:00");

const failure = parseIdentityReceipt(
  '{"schema":"aap-identity/v1","ok":false,"error":{"code":"credential_revoked"}}',
  200,
);
assert.deepEqual(failure.error, { code: "credential_revoked" });

for (const malformed of [
  success.replace('"generation":9223372036854775807', '"generation":1.0'),
  success.replace('"generation":9223372036854775807', '"generation":9223372036854775808'),
  success.replace('"role":"viewer"', '"role":"unknown"'),
  success.replace('"schema":"aap-identity/v1"', '"schema":"other/v1"'),
  success.replace('"is_active":true', '"is_active":1'),
  success.replace('"credential_id":"00000000-0000-0000-0000-000000000002",', ""),
]) {
  assert.throws(() => parseIdentityReceipt(malformed, 200));
}

process.stdout.write("Identity admission peer self-check passed\n");
