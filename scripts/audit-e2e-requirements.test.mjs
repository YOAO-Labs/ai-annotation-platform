import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { auditE2ERequirements, main } from "./audit-e2e-requirements.mjs";

test("passed, failed, cancelled, setup-failure and missing are accounted fail-closed", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-"));
  try {
    writeFileSync(
      join(dir, "smoke.json"),
      JSON.stringify({
        suite: "smoke",
        outcome: "success",
        stats: { expected: 9, unexpected: 0, flaky: 0, skipped: 0 },
      }),
    );
    writeFileSync(
      join(dir, "default-one.json"),
      JSON.stringify({ suite: "default-one", outcome: "failure" }),
    );
    writeFileSync(
      join(dir, "mask-native.json"),
      JSON.stringify({ suite: "mask-native", outcome: "cancelled" }),
    );
    writeFileSync(
      join(dir, "pointcloud.json"),
      JSON.stringify({ suite: "pointcloud", outcome: "setup-failure" }),
    );
    const audit = auditE2ERequirements({
      requiredSuites: [
        { suite: "smoke", planned: true },
        { suite: "default-one", planned: true },
        { suite: "mask-native", planned: true },
        { suite: "pointcloud", planned: true },
        { suite: "layout-stress", planned: true },
      ],
      statusDir: dir,
    });
    assert.equal(audit.ok, false);
    const bySuite = Object.fromEntries(audit.rows.map((row) => [row.suite, row.state]));
    assert.equal(bySuite.smoke, "passed");
    assert.equal(bySuite["default-one"], "failed");
    assert.equal(bySuite["mask-native"], "cancelled");
    assert.equal(bySuite.pointcloud, "setup-failure");
    assert.equal(bySuite["layout-stress"], "missing");
    assert.deepEqual(audit.blockers.map((row) => row.suite).sort(), [
      "default-one",
      "layout-stress",
      "mask-native",
      "pointcloud",
    ]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("docs-allowed skips never block while unplanned skips do", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-"));
  try {
    writeFileSync(
      join(dir, "smoke.json"),
      JSON.stringify({
        suite: "smoke",
        outcome: "success",
        stats: { expected: 9, unexpected: 0, flaky: 0, skipped: 0 },
      }),
    );
    writeFileSync(
      join(dir, "mask-native.json"),
      JSON.stringify({ suite: "mask-native", outcome: "skipped" }),
    );
    const withReason = auditE2ERequirements({
      requiredSuites: {
        classification: "docs-only",
        reason: "docs-only change (whitelisted markdown/docs-site paths); no app E2E required",
        suites: [
          { suite: "smoke", planned: true },
          {
            suite: "mask-native",
            planned: false,
            docsAllowedSkip: true,
            reason: "dedicated matrix intentionally not run",
          },
        ],
      },
      statusDir: dir,
    });
    assert.equal(withReason.ok, true);
    assert.deepEqual(withReason.rows.map((row) => row.state).sort(), [
      "docs-allowed-skip",
      "passed",
    ]);
    const withoutReason = auditE2ERequirements({
      requiredSuites: [
        { suite: "smoke", planned: true },
        { suite: "mask-native", planned: true },
      ],
      statusDir: dir,
    });
    assert.equal(withoutReason.ok, false);
    assert.deepEqual(withoutReason.blockers.map((row) => row.suite).sort(), ["mask-native"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("empty runtime required lists fail and an explained docs-only skip is idempotent", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-"));
  try {
    const invalid = auditE2ERequirements({ requiredSuites: [], statusDir: dir });
    assert.equal(invalid.ok, false);
    assert.equal(invalid.blockers[0].state, "invalid-manifest");

    const manifest = {
      classification: "docs-only",
      reason: "docs-only change (whitelisted markdown/docs-site paths); no app E2E required",
      suites: [],
    };
    const first = auditE2ERequirements({ requiredSuites: manifest, statusDir: dir });
    const again = auditE2ERequirements({ requiredSuites: manifest, statusDir: dir });
    assert.equal(first.ok, true);
    assert.equal(again.ok, true);
    assert.deepEqual(first.rows, again.rows);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the CLI accepts an empty test-only manifest only with an explicit nonblank reason", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-test-only-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, "required-suites.json");
  const output = [];
  const io = {
    readFileSync,
    stdout: { write: (message) => output.push(message) },
  };
  const run = (manifest) => {
    writeFileSync(path, JSON.stringify(manifest));
    return main([path, dir], io);
  };

  const skip = {
    classification: "test-only",
    reason: "Only isolated Vitest cases changed.",
    suites: [],
  };
  assert.equal(run(skip), 0);
  assert.match(output.pop(), /No browser suites required \(test-only\): Only isolated Vitest/);

  for (const invalid of [
    { ...skip, reason: "" },
    { ...skip, reason: "  " },
    { ...skip, reason: true },
    { ...skip, classification: "app-code" },
    { ...skip, classification: "unit-only" },
    { ...skip, suites: ["smoke"] },
    { ...skip, suites: [{ suite: "smoke", planned: false, reason: "not run" }] },
  ]) {
    assert.equal(run(invalid), 1, JSON.stringify(invalid));
    assert.match(output.pop(), /invalid-manifest/);
  }
});

test("docs-allowed skip flags cannot bypass a planned:true required suite", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-bypass-"));
  try {
    const bypass = auditE2ERequirements({
      requiredSuites: {
        classification: "docs-only",
        reason: "documentation update",
        suites: [{ suite: "smoke", planned: true, docsAllowedSkip: true, reason: "docs only" }],
      },
      statusDir: dir,
    });
    assert.equal(bypass.ok, false);
    assert.deepEqual(
      bypass.blockers.map((row) => row.state),
      ["invalid-manifest"],
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("CLI distinguishes missing arguments, manifest and status directory", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-inputs-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const manifest = join(dir, "required.json");
  const status = join(dir, "status");
  const stderr = [];
  const io = { stderr: { write: (message) => stderr.push(message) } };
  assert.equal(main([], io), 2);
  assert.match(stderr.pop(), /usage:/);
  assert.equal(main([manifest, status], io), 2);
  assert.ok(stderr.pop().includes(manifest));
  writeFileSync(manifest, "[]");
  assert.equal(main([manifest, status], io), 2);
  assert.ok(stderr.pop().includes(status));
});

test("audit rejects malformed completion artifacts fail-closed", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-malformed-"));
  try {
    writeFileSync(
      join(dir, "smoke.json"),
      JSON.stringify({
        suite: "smoke",
        outcome: "success",
        stats: { expected: 1, unexpected: 0, flaky: 0, skipped: 0 },
      }),
    );
    writeFileSync(
      join(dir, "default-one.json"),
      JSON.stringify({ suite: "default-one", outcome: "success" }),
    );
    writeFileSync(
      join(dir, "mask-native.json"),
      JSON.stringify({
        suite: "some-other-suite",
        outcome: "success",
        stats: { expected: 1, unexpected: 0, flaky: 0, skipped: 0 },
      }),
    );
    writeFileSync(join(dir, "mask-readonly.json"), "{not json");
    writeFileSync(
      join(dir, "layout-stress.json"),
      JSON.stringify({ suite: "layout-stress", outcome: "hitched-a-ride" }),
    );
    const audit = auditE2ERequirements({
      requiredSuites: [
        { suite: "smoke", planned: true },
        { suite: "default-one", planned: true },
        { suite: "mask-native", planned: true },
        { suite: "mask-readonly", planned: true },
        { suite: "layout-stress", planned: true },
      ],
      statusDir: dir,
    });
    assert.equal(audit.ok, false);
    const bySuite = Object.fromEntries(audit.rows.map((row) => [row.suite, row.state]));
    assert.equal(bySuite.smoke, "passed");
    assert.equal(bySuite["default-one"], "malformed-success");
    assert.equal(bySuite["mask-native"], "suite-mismatch");
    assert.equal(bySuite["mask-readonly"], "unreadable");
    assert.equal(bySuite["layout-stress"], "unknown-outcome");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a nonempty runtime manifest cannot pass on an all-skipped or failed receipt", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-runtime-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const suite = "layout-short";
  const requiredSuites = {
    classification: "app-code",
    reason: "dock layout changed",
    suites: [suite],
  };
  for (const [receipt, state] of [
    [{ outcome: "skipped" }, "skipped-not-run"],
    [{ outcome: "failure" }, "failed"],
    [
      { outcome: "success", stats: { expected: 0, unexpected: 0, flaky: 0, skipped: 3 } },
      "malformed-success",
    ],
    [
      { outcome: "success", stats: { expected: 1, unexpected: 1, flaky: 0, skipped: 0 } },
      "malformed-success",
    ],
  ]) {
    writeFileSync(join(dir, `${suite}.json`), JSON.stringify({ suite, ...receipt }));
    const audit = auditE2ERequirements({ requiredSuites, statusDir: dir });
    assert.equal(audit.ok, false);
    assert.deepEqual(
      audit.blockers.map((row) => row.state),
      [state],
    );
  }
});

test("array manifest cannot smuggle a docs-allowed skip (exact negative probe)", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-array-bypass-"));
  try {
    const audit = auditE2ERequirements({
      requiredSuites: [{ suite: "x", planned: false, docsAllowedSkip: true, reason: "docs only" }],
      statusDir: dir,
    });
    assert.equal(audit.ok, false);
    assert.deepEqual(
      audit.blockers.map((row) => row.state),
      ["invalid-manifest"],
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("duplicate suite names and blank docs-only reasons fail closed", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-dup-"));
  try {
    const duplicate = auditE2ERequirements({
      requiredSuites: [
        { suite: "smoke", planned: true },
        { suite: "smoke", planned: true },
      ],
      statusDir: dir,
    });
    assert.equal(duplicate.ok, false);
    assert.ok(duplicate.blockers.some((row) => /duplicate suite: smoke/.test(row.detail)));

    const blankReason = auditE2ERequirements({
      requiredSuites: {
        classification: "docs-only",
        reason: "   ",
        suites: [{ suite: "smoke", planned: false, docsAllowedSkip: true, reason: "skip" }],
      },
      statusDir: dir,
    });
    assert.equal(blankReason.ok, false);
    assert.ok(blankReason.blockers.some((row) => /nonblank reason/.test(row.detail)));

    const blankName = auditE2ERequirements({
      requiredSuites: {
        classification: "app-code",
        reason: "gate",
        suites: [{ suite: "  ", planned: true }],
      },
      statusDir: dir,
    });
    assert.equal(blankName.ok, false);
    assert.ok(blankName.blockers.some((row) => /suite name missing or blank/.test(row.detail)));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("core-flaky policy blocks a forbid-flaky suite that only passed on retry", () => {
  const dir = mkdtempSync(join(tmpdir(), "e2e-audit-flaky-"));
  try {
    writeFileSync(
      join(dir, "smoke.json"),
      JSON.stringify({
        suite: "smoke",
        outcome: "success",
        stats: { expected: 9, unexpected: 0, flaky: 1, skipped: 0 },
      }),
    );
    writeFileSync(
      join(dir, "mask-native.json"),
      JSON.stringify({
        suite: "mask-native",
        outcome: "success",
        stats: { expected: 20, unexpected: 0, flaky: 1, skipped: 2 },
      }),
    );
    const blocked = auditE2ERequirements({
      requiredSuites: [
        { suite: "smoke", planned: true, flakyPolicy: "forbid" },
        { suite: "mask-native", planned: true, flakyPolicy: "diagnostic" },
      ],
      statusDir: dir,
    });
    assert.equal(blocked.ok, false);
    assert.deepEqual(
      blocked.blockers.map((row) => [row.suite, row.state]),
      [["smoke", "core-flaky"]],
    );

    const allowed = auditE2ERequirements({
      requiredSuites: [{ suite: "mask-native", planned: true, flakyPolicy: "diagnostic" }],
      statusDir: dir,
    });
    assert.equal(allowed.ok, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
