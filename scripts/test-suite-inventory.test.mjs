import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { auditTestRoots } from "./test-suite-inventory.mjs";

function withRoots(paths, check) {
  const root = mkdtempSync(join(tmpdir(), "test-suite-inventory-"));
  try {
    for (const path of paths) mkdirSync(join(root, path), { recursive: true });
    check(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("a new owner test root cannot silently escape CI ownership", () => {
  withRoots(["apps/jobs/tests", "apps/new-owner/tests"], (root) => {
    const inventory = auditTestRoots(root, {
      ml_cpu: [],
      owner_contracts: [{ directory: "apps/jobs" }],
      other_test_roots: [],
    });
    assert.deepEqual(inventory.unmapped, ["apps/new-owner/tests"]);
  });
});

test("independent contracts, plugins and explicitly manual experiments retain distinct owners", () => {
  withRoots(
    [
      "apps/_shared/task_contract/tests",
      "plugins/libreyolo/tests",
      "packages/sdk/tests",
      "apps/api/experiments/tests",
      "apps/yolo-backend/vendor/upstream/tests",
    ],
    (root) => {
      const inventory = auditTestRoots(root, {
        ml_cpu: [{ paths: ["apps/_shared/task_contract/", "plugins/libreyolo/"] }],
        owner_contracts: [],
        other_test_roots: [{ paths: ["packages/sdk/tests", "apps/api/experiments/tests"] }],
      });
      assert.deepEqual(inventory.unmapped, []);
      assert.deepEqual(inventory.discovered, [
        "apps/_shared/task_contract/tests",
        "apps/api/experiments/tests",
        "packages/sdk/tests",
        "plugins/libreyolo/tests",
      ]);
    },
  );
});
