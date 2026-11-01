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

test("a completed first attempt has no invented retry or failure", () => {
  const { status } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("passed"),
  });
  assert.equal(status.attempts.firstAttemptPassed, 1);
  assert.equal(status.attempts.retriedPassed, 0);
  assert.equal(status.attempts.failed, 0);
  assert.equal(status.firstFailure, null);
});

test("failed attempts retain the first failure rather than the retry reason", () => {
  const report = loadFixture("failed");
  const attempts = report.suites[0].specs[0].tests[0].results;
  attempts[0].error.message = "first-attempt-input";
  attempts[1].error.message = "retry-input";
  const { status } = buildSummary({ suite: "default-one", outcome: "failure", report });
  assert.equal(status.attempts.failed, 1);
  assert.equal(status.attempts.timeout, 0);
  assert.equal(status.firstFailure.reason, "first-attempt-input");
  assert.equal(status.firstFailure.retry, 0);
  assert.equal(status.firstFailure.retryOutcome, "failed");
});

test("a retry pass is distinct from a first-attempt pass", () => {
  const { status } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("flaky"),
  });
  assert.equal(status.attempts.firstAttemptPassed, 0);
  assert.equal(status.attempts.retriedPassed, 1);
  assert.equal(status.firstFailure.retryOutcome, "passed");
});

test("an executed expected failure is not an unexpected failure", () => {
  const { status } = buildSummary({
    suite: "smoke",
    outcome: "success",
    report: loadFixture("expected-failure"),
  });
  assert.equal(status.attempts.firstAttemptPassed, 1);
  assert.equal(status.attempts.failed, 0);
});

test("an executed skip is distinct from a case without any attempts", () => {
  const report = loadFixture("skipped");
  const executed = buildSummary({ suite: "smoke", outcome: "success", report }).status;
  assert.equal(executed.attempts.intentionalSkipped, 1);
  assert.equal(executed.attempts.notRun, 0);
  report.suites[0].specs[0].tests[0].results = [];
  const unattempted = buildSummary({ suite: "smoke", outcome: "success", report }).status;
  assert.equal(unattempted.attempts.intentionalSkipped, 0);
  assert.equal(unattempted.attempts.notRun, 1);
});

test("max-failures accounts for the cases the runner actually attempted", () => {
  const { status } = buildSummary({
    suite: "default-one",
    outcome: "failure",
    report: loadFixture("maxfailures"),
  });
  assert.equal(status.attempts.firstAttemptPassed, 1);
  assert.equal(status.attempts.failed, 1);
  assert.equal(status.attempts.notRun, 0);
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
  assert.equal(status.outcome, "success");
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

for (const [fixture, expected] of [
  ["passed", 0],
  ["skipped", 1],
  ["failed", 1],
  ["maxfailures", 1],
]) {
  test(`required browser integration rejects incomplete execution: ${fixture}`, () => {
    const directory = mkdtempSync(join(tmpdir(), "e2e-required-worker-"));
    try {
      const reportPath = join(fixtureDir.pathname, `${fixture}.json`);
      const statusPath = join(directory, "worker.json");
      const result = spawnSync(
        process.execPath,
        [
          fileURLToPath(new URL("./summarize-e2e-results.mjs", import.meta.url)),
          "mask-worker",
          "success",
          reportPath,
        ],
        {
          encoding: "utf8",
          env: {
            ...process.env,
            E2E_REQUIRE_ALL_TESTS: "1",
            E2E_STATUS_OUT: statusPath,
            GITHUB_STEP_SUMMARY: "",
          },
        },
      );
      assert.equal(result.status, expected, result.stderr);
      assert.equal(
        JSON.parse(readFileSync(statusPath, "utf8")).outcome,
        expected ? "failure" : "success",
      );
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

test("required execution rejects positive counters without any case records", () => {
  const directory = mkdtempSync(join(tmpdir(), "e2e-empty-execution-"));
  try {
    const report = loadFixture("passed");
    report.suites = [];
    const reportPath = join(directory, "report.json");
    const statusPath = join(directory, "status.json");
    writeFileSync(reportPath, JSON.stringify(report));
    const result = spawnSync(
      process.execPath,
      [
        fileURLToPath(new URL("./summarize-e2e-results.mjs", import.meta.url)),
        "mask-worker",
        "success",
        reportPath,
      ],
      {
        encoding: "utf8",
        env: {
          ...process.env,
          E2E_REQUIRE_ALL_TESTS: "1",
          E2E_STATUS_OUT: statusPath,
          GITHUB_STEP_SUMMARY: "",
        },
      },
    );
    assert.equal(result.status, 1, result.stderr);
    assert.equal(JSON.parse(readFileSync(statusPath, "utf8")).outcome, "failure");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
