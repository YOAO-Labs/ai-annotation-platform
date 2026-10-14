#!/usr/bin/env node
/**
 * Summarize one Playwright E2E suite run for the GitHub step summary.
 *
 * Reads the Playwright JSON report plus run metadata and renders:
 * - pass/fail/flaky/skipped counts and duration (as before), plus
 * - first-attempt vs retried outcomes, timeout / interrupted / not-run
 *   classification, global errors,
 * - a REQUIRED-MISSING marker when the suite was planned but produced no
 *   report while the run step claims success (fail-closed),
 * - preparation and execution wall-clock seconds when the caller supplies
 *   E2E_PREP_SECONDS / E2E_RUN_SECONDS.
 *
 * The stable `Frontend E2E` gate semantics are unchanged: only the reporting
 * fidelity improves.
 */

import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";

// Classification contract (locked Playwright 1.59 JSON report,
// playwright/types/testReporter.d.ts): JSONReportTest.status is
// 'skipped' | 'expected' | 'unexpected' | 'flaky' and JSONReportSpec carries
// `ok` — JSONReportTest has NO `ok`. Results carry TestStatus
// 'passed' | 'failed' | 'timedout' | 'skipped' | 'interrupted' (lowercase).
// Verified against real generated runs: scripts/fixtures/e2e-summary/*.json.
function summarizeTests(report) {
  let firstAttemptPassed = 0;
  let retriedPassed = 0;
  let failed = 0;
  let timeout = 0;
  let interrupted = 0;
  let intentionalSkipped = 0;
  let notRun = 0;
  let firstFailure = null;

  const visit = (suite) => {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        const attempts = test.results ?? [];
        const last = attempts[attempts.length - 1];
        // An intentional skip executes a skipped result; a test the runner
        // never attempted has an empty results array.
        if (test.status === "skipped" && attempts.some((result) => result.status === "skipped")) {
          intentionalSkipped += 1;
          continue;
        }
        if (attempts.length === 0) {
          notRun += 1;
          continue;
        }
        const lastStatus = last?.status;
        switch (test.status) {
          case "expected":
            firstAttemptPassed += 1;
            break;
          case "flaky":
            retriedPassed += 1;
            break;
          case "unexpected":
            failed += 1;
            if (lastStatus === "timedout") timeout += 1;
            if (lastStatus === "interrupted") interrupted += 1;
            break;
          default:
            break;
        }
        // §6.6: the FIRST failed attempt carries the failure evidence —
        // including flaky tests, whose final status alone would hide it.
        if (!firstFailure && (test.status === "unexpected" || test.status === "flaky")) {
          const firstAttempt = attempts[0];
          firstFailure = {
            id: spec.id,
            file: spec.file,
            title: spec.title,
            projectId: test.projectId,
            status: test.status,
            reason:
              (firstAttempt?.error?.message ?? "")
                .replace(/\x1b\[[0-9;]*m/g, "")
                .split("\n")
                .find((line) => line.trim())
                ?.trim() ??
              firstAttempt?.status ??
              "unknown",
            retry: firstAttempt?.retry ?? 0,
            retryOutcome: attempts.length > 1 ? (last?.status ?? "unknown") : "none",
          };
        }
      }
    }
    for (const child of suite.suites ?? []) visit(child);
  };
  for (const suite of report.suites ?? []) visit(suite);
  return {
    firstAttemptPassed,
    retriedPassed,
    failed,
    timeout,
    interrupted,
    intentionalSkipped,
    notRun,
    firstFailure,
  };
}

export function buildSummary({ suite, outcome, report, prepSeconds, runSeconds, buildSeconds }) {
  const heading = `### ${suite}\n\nOutcome: ${outcome}\n\n`;
  if (!report) {
    return {
      text:
        heading +
        "No completed Playwright report. Inspect setup, server startup or timeout logs.\n",
      requiredMissing: true,
      status: {
        suite,
        // A skipped run after failed preparation is not an allowed suite skip.
        // Never publish a success artifact without the report that proves it.
        outcome:
          outcome === "skipped" ? "setup-failure" : outcome === "success" ? "failure" : outcome,
        runOutcome: outcome,
        reportMissing: true,
      },
    };
  }
  const { stats, errors } = report;
  const attempts = summarizeTests(report);
  const duration = (stats.duration / 1000).toFixed(1);
  const timing =
    prepSeconds !== undefined && runSeconds !== undefined
      ? `\nPrep: ${Number(prepSeconds).toFixed(0)}s · Execution: ${Number(runSeconds).toFixed(0)}s${buildSeconds !== undefined ? ` · Build: ${Number(buildSeconds).toFixed(0)}s` : ""}\n`
      : "";
  const firstFailureLine = attempts.firstFailure
    ? `\nFirst failure: ${attempts.firstFailure.file} › ${attempts.firstFailure.title} [${attempts.firstFailure.id}] (project ${attempts.firstFailure.projectId}, attempt ${attempts.firstFailure.retry}) — ${attempts.firstFailure.reason} [retry outcome: ${attempts.firstFailure.retryOutcome}]\n`
    : "";
  const text =
    heading +
    `| Passed | Failed | Flaky | Skipped | Global errors | Duration |\n| --- | --- | --- | --- | --- | --- |\n| ${stats.expected} | ${stats.unexpected} | ${stats.flaky} | ${stats.skipped} | ${errors.length} | ${duration} |\n` +
    `| First-attempt passed | Retry-then-passed | Timed out | Interrupted | Intentionally skipped | Not run |\n| --- | --- | --- | --- | --- | --- |\n| ${attempts.firstAttemptPassed} | ${attempts.retriedPassed} | ${attempts.timeout} | ${attempts.interrupted} | ${attempts.intentionalSkipped} | ${attempts.notRun} |\n` +
    firstFailureLine +
    timing;
  return {
    text,
    requiredMissing: false,
    status: {
      suite,
      outcome,
      attempts,
      stats: {
        expected: stats.expected,
        unexpected: stats.unexpected,
        flaky: stats.flaky,
        skipped: stats.skipped,
      },
      firstFailure: attempts.firstFailure ?? null,
    },
  };
}

export function main(argv, env, io) {
  const [suite, outcome] = argv;
  if (!suite || !outcome) {
    io.stderr.write("usage: summarize-e2e-results.mjs <suite> <outcome> [reportPath]\n");
    return 2;
  }
  const reportPath = argv[2] ?? "apps/web/e2e-results.json";
  const statusOut = env.E2E_STATUS_OUT;
  const report = io.existsSync(reportPath) ? JSON.parse(io.readFileSync(reportPath, "utf8")) : null;
  const { text, requiredMissing, status } = buildSummary({
    suite,
    outcome,
    report,
    prepSeconds: env.E2E_PREP_SECONDS,
    runSeconds: env.E2E_RUN_SECONDS,
    buildSeconds: env.E2E_BUILD_SECONDS,
  });
  if (statusOut && status)
    io.writeFileSync(statusOut, JSON.stringify({ suite, outcome, ...status }, null, 1));
  if (io.summaryPath) io.appendFileSync(io.summaryPath, text);
  else io.stdout.write(text);
  // Fail-closed: a planned suite that claims success without a report must
  // fail the step so the missing evidence is never read as a pass.
  if (requiredMissing && outcome === "success") {
    io.stderr.write(`required suite ${suite} reported success without a Playwright report\n`);
    return 1;
  }
  return 0;
}

export function cli() {
  const io = {
    stdout: process.stdout,
    stderr: process.stderr,
    appendFileSync,
    readFileSync,
    writeFileSync,
    existsSync,
    summaryPath: process.env.GITHUB_STEP_SUMMARY,
  };
  return main(process.argv.slice(2), process.env, io);
}

if (
  process.argv[1] &&
  import.meta.url === (await import("node:url")).pathToFileURL(process.argv[1]).href
) {
  process.exit(cli());
}
