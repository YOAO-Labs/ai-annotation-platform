import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildSummary, main } from "./summarize-e2e-results.mjs";
import { auditE2ERequirements } from "./audit-e2e-requirements.mjs";

// Fixtures are REAL Playwright JSON reports (locked 1.59.1 contract), generated
// from tiny controlled runs: scripts/fixtures/e2e-summary/*.json. See the
// generation commands in docs/research/36 §3.
const fixtureDir = new URL("./fixtures/e2e-summary/", import.meta.url);
const loadFixture = (name) => JSON.parse(readFileSync(new URL(`${name}.json`, fixtureDir), "utf8"));

test("passed fixture: one first-attempt pass, no retries or failures", () => {
  const { text } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("passed"),
  });
  assert.match(text, /\| 1 \| 0 \| 0 \| 0 \| 0 \|/);
  assert.match(text, /First-attempt passed \| Retry-then-passed/);
  assert.doesNotMatch(text, /First failure:/);
});

test("failed fixture: unexpected status, timeout-free failure and a recorded first failure with id and reason", () => {
  const { text } = buildSummary({
    suite: "default-one",
    outcome: "failure",
    report: loadFixture("failed"),
  });
  assert.match(text, /\| 0 \| 1 \| 0 \| 0 \| 0 \| 5\.7 \|/);
  assert.match(
    text,
    /First failure: failed\.spec\.ts › fails with a stable reason \[.+\] \(project chromium, attempt 0\) — Error: expect\(received\)\.toBe\(expected\)/,
  );
  // Both attempts failed (retries:1), so the retry outcome is a failure too.
  assert.match(text, /\[retry outcome: failed\]/);
});

test("flaky fixture: retry-then-passed is counted separately from first-attempt", () => {
  const { text } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("flaky"),
  });
  assert.match(text, /First-attempt passed \| Retry-then-passed/);
  assert.match(text, /\| 0 \| 1 \|/);
});

test("expected-failure fixture counts as expected, not as a failure", () => {
  const { text } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("expected-failure"),
  });
  assert.match(text, /\| 1 \| 0 \| 0 \| 0 \|/);
});

test("skipped fixture is intentional (executed skip result), not not-run", () => {
  const { text } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("skipped"),
  });
  assert.match(text, /\| 0 \| 0 \| 0 \| 1 \|/);
  assert.match(text, /\| 0 \| 0 \| 0 \| 0 \| 1 \| 0 \|/);
});

test("max-failures fixture keeps per-test accounting without fabricating not-run entries", () => {
  // Real 1.59 runs with --max-failures still execute and report the queued
  // test (worker already started), so notRun stays 0 and the interruption is
  // attributed by status; missing suites are covered by the audit instead.
  const { text } = buildSummary({
    suite: "default-one",
    outcome: "failure",
    report: loadFixture("maxfailures"),
  });
  assert.match(text, /\| 1 \| 1 \|/);
  assert.match(text, /Not run \|/);
});

test("timing rows render prep/execution/build seconds when supplied", () => {
  const { text } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("passed"),
    prepSeconds: "210",
    runSeconds: "95",
    buildSeconds: "77",
  });
  assert.match(text, /Prep: 210s · Execution: 95s · Build: 77s/);
});

test("a missing report with a success outcome fails closed", () => {
  const writes = [];
  const stderr = [];
  const code = main(
    ["mask-native", "success", "missing-e2e-results.json"],
    {},
    {
      stdout: [],
      stderr: { write: (chunk) => stderr.push(chunk) },
      appendFileSync: (path, chunk) => writes.push([path, chunk]),
      readFileSync: () => {
        throw new Error("should not read");
      },
      existsSync: () => false,
      summaryPath: "summary.md",
    },
  );
  assert.equal(code, 1);
  assert.ok(writes[0][1].includes("No completed Playwright report"));
  assert.ok(stderr.join("").includes("without a Playwright report"));
});

test("a missing report with a failed outcome is reported without failing the step again", () => {
  const stderr = [];
  const writes = [];
  const code = main(
    ["mask-native", "failure", "missing-e2e-results.json"],
    {},
    {
      stdout: { write: (chunk) => writes.push(chunk) },
      stderr: { write: (chunk) => stderr.push(chunk) },
      appendFileSync: () => {},
      readFileSync: () => "",
      existsSync: () => false,
      summaryPath: undefined,
    },
  );
  assert.equal(code, 0);
});

test("usage error exits 2", () => {
  const code = main([], {}, { stdout: [], stderr: { write: () => {} } });
  assert.equal(code, 2);
});

test("CLI subprocess writes a parseable status artifact and exits 0", () => {
  const script = fileURLToPath(new URL("./summarize-e2e-results.mjs", import.meta.url));
  const outDir = mkdtempSync(join(tmpdir(), "e2e-summary-cli-"));
  const statusDir = join(outDir, "suite-status");
  mkdirSync(statusDir, { recursive: true });
  const report = loadFixture("passed");
  const reportPath = join(outDir, "e2e-results.json");
  writeFileSync(reportPath, JSON.stringify(report));
  const summaryPath = join(outDir, "summary.md");
  const result = spawnSync(process.execPath, [script, "smoke", "success", reportPath], {
    encoding: "utf8",
    env: {
      ...process.env,
      E2E_STATUS_OUT: join(statusDir, "smoke.json"),
      GITHUB_STEP_SUMMARY: summaryPath,
    },
  });
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(readFileSync(join(statusDir, "smoke.json"), "utf8"));
  assert.equal(status.suite, "smoke");
  assert.equal(status.outcome, "success");
  assert.equal(typeof status.stats.expected, "number");
  assert.ok(readFileSync(summaryPath, "utf8").includes("Outcome: success"));
  rmSync(outDir, { recursive: true, force: true });
});

for (const [runOutcome, artifactOutcome, exitCode] of [
  ["skipped", "setup-failure", 0],
  ["success", "failure", 1],
  ["failure", "failure", 0],
  ["cancelled", "cancelled", 0],
]) {
  test(`missing report: ${runOutcome} publishes ${artifactOutcome} and fails the suite audit`, () => {
    const script = fileURLToPath(new URL("./summarize-e2e-results.mjs", import.meta.url));
    const outDir = mkdtempSync(join(tmpdir(), "e2e-summary-missing-"));
    try {
      const statusPath = join(outDir, "smoke.json");
      const result = spawnSync(
        process.execPath,
        [script, "smoke", runOutcome, join(outDir, "missing-report.json")],
        {
          encoding: "utf8",
          env: { ...process.env, E2E_STATUS_OUT: statusPath, GITHUB_STEP_SUMMARY: "" },
        },
      );
      assert.equal(result.status, exitCode, result.stderr);
      assert.deepEqual(JSON.parse(readFileSync(statusPath, "utf8")), {
        suite: "smoke",
        outcome: artifactOutcome,
        runOutcome,
        reportMissing: true,
      });
      assert.match(result.stdout, /No completed Playwright report/);
      const audit = auditE2ERequirements({
        requiredSuites: [{ suite: "smoke", planned: true }],
        statusDir: outDir,
      });
      assert.equal(audit.ok, false);
      assert.equal(
        audit.blockers[0].state,
        artifactOutcome === "failure" ? "failed" : artifactOutcome,
      );
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  });
}
