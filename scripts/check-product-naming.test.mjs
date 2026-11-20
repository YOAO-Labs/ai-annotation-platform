import assert from "node:assert/strict";
import test from "node:test";
import { violations } from "./check-product-naming.mjs";

function diff(path, body) {
  return `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n${body}`;
}

test("flags a newly broken relay executable at its new-file line, not its removal", () => {
  const relay = "docker-compose.jobs-dispatch-relay.yml";
  const offending = "    command: [aap-jobs-dispatch-relay]";
  assert.deepEqual(violations(diff(relay, `@@ -6,0 +7 @@\n+${offending}`)), [
    `${relay}:7: use the installed yoao-* executable`,
  ]);
  assert.deepEqual(
    violations(diff(relay, `@@ -7 +7 @@\n-${offending}\n+    command: [yoao-jobs-dispatch-relay]`)),
    [],
  );
});

test("allows historical fixtures and runtime identities without exempting adjacent code", () => {
  const file = "apps/api/app/services/example.py";
  const patch =
    diff("apps/api/tests/fixtures/old.py", "@@ -0,0 +1 @@\n+import aap_sdk") +
    "\n" +
    diff(
      file,
      "@@ -8,0 +9,3 @@\n+token = env['AAP_PLUGIN_TOKEN']\n+++ b/not-a-patch-header\n+import aap_sdk",
    );
  assert.deepEqual(violations(patch), [`${file}:11: import the published yoao package`]);
});
