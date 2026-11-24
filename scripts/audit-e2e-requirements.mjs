#!/usr/bin/env node

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export function validateRequiredManifest(manifest) {
  const errors = [];
  let classification = "app-code";
  let reason = "";
  let rawSuites;
  if (Array.isArray(manifest)) {
    rawSuites = manifest.map((entry) =>
      typeof entry === "string" ? { suite: entry, planned: true } : { planned: true, ...entry },
    );
  } else if (manifest && typeof manifest === "object" && Array.isArray(manifest.suites)) {
    classification = manifest.classification ?? "app-code";
    reason = manifest.reason ?? "";
    rawSuites = manifest.suites.map((entry) =>
      typeof entry === "string" ? { suite: entry, planned: true } : { planned: true, ...entry },
    );
  } else {
    return {
      classification: "invalid",
      reason: "",
      suites: [],
      errors: ["manifest must be a suite array or {classification, reason, suites}"],
    };
  }
  const mayBeEmpty = classification === "docs-only" || classification === "test-only";
  if (!mayBeEmpty && classification !== "app-code")
    errors.push(`unknown required manifest classification: ${classification}`);
  if (!mayBeEmpty && rawSuites.length === 0)
    errors.push("empty required list without a docs-only or test-only classification");
  if (mayBeEmpty && (typeof reason !== "string" || !reason.trim()))
    errors.push(`${classification} manifest requires a nonblank reason`);
  if (classification === "test-only" && rawSuites.length > 0)
    errors.push("test-only manifest requires an empty suite list");

  const suites = [];
  const seen = new Set();
  for (const raw of rawSuites) {
    const suite = typeof raw.suite === "string" ? raw.suite.trim() : "";
    if (!suite) {
      errors.push("suite name missing or blank");
      continue;
    }
    if (seen.has(suite)) {
      errors.push(`duplicate suite: ${suite}`);
      continue;
    }
    seen.add(suite);
    const docsAllowedSkip = raw.docsAllowedSkip === true;
    if (docsAllowedSkip && classification !== "docs-only")
      errors.push(`${suite}: docsAllowedSkip is only valid in a docs-only manifest`);
    if (docsAllowedSkip && raw.planned !== false)
      errors.push(`${suite}: docsAllowedSkip requires planned:false`);
    if (docsAllowedSkip && !String(raw.reason ?? "").trim())
      errors.push(`${suite}: docs-allowed skip requires an explicit reason`);
    if (raw.flakyPolicy !== undefined && !["forbid", "diagnostic"].includes(raw.flakyPolicy))
      errors.push(`${suite}: unknown flakyPolicy ${raw.flakyPolicy}`);
    suites.push({
      suite,
      planned: raw.planned ?? true,
      docsAllowedSkip: docsAllowedSkip && classification === "docs-only",
      reason: raw.reason,
      flakyPolicy: raw.flakyPolicy,
    });
  }
  return { classification, reason, suites, errors };
}

export function auditE2ERequirements({ requiredSuites: requiredInput, statusDir }) {
  const manifest = validateRequiredManifest(requiredInput);
  if (manifest.errors.length > 0)
    return {
      rows: [],
      blockers: manifest.errors.map((detail) => ({
        suite: "(manifest)",
        state: "invalid-manifest",
        detail,
      })),
      ok: false,
    };
  const rows = [];
  for (const entry of manifest.suites) {
    const suite = entry.suite;
    const docsAllowedSkip = entry.docsAllowedSkip && entry.planned === false;
    const file = join(statusDir, `${suite}.json`);
    if (!existsSync(file)) {
      rows.push({ suite, state: docsAllowedSkip ? "docs-allowed-skip" : "missing" });
      continue;
    }
    let status;
    try {
      status = JSON.parse(readFileSync(file, "utf8"));
    } catch (error) {
      rows.push({ suite, state: "unreadable", detail: String(error) });
      continue;
    }
    if (status.suite !== suite) {
      rows.push({ suite, state: "suite-mismatch", detail: status.suite ?? null });
      continue;
    }
    if (
      !["success", "failure", "timedout", "cancelled", "setup-failure", "skipped"].includes(
        status.outcome,
      )
    ) {
      rows.push({ suite, state: "unknown-outcome", detail: status.outcome ?? null });
      continue;
    }
    if (status.outcome === "success") {
      const stats = status.stats ?? {};
      const numeric = ["expected", "unexpected", "flaky", "skipped"].every(
        (key) => Number.isSafeInteger(stats[key]) && stats[key] >= 0,
      );
      if (!numeric || stats.expected + stats.flaky === 0 || stats.unexpected > 0) {
        rows.push({
          suite,
          state: "malformed-success",
          detail: "success requires passing stats with at least one executed case and no failures",
        });
        continue;
      }
      if (entry.flakyPolicy === "forbid" && stats.flaky > 0) {
        rows.push({ suite, state: "core-flaky", detail: { flaky: stats.flaky } });
        continue;
      }
      rows.push({ suite, state: "passed", detail: status });
      continue;
    }
    if (status.outcome === "skipped") {
      rows.push({ suite, state: docsAllowedSkip ? "docs-allowed-skip" : "skipped-not-run" });
      continue;
    }
    rows.push({
      suite,
      state:
        status.outcome === "cancelled"
          ? "cancelled"
          : status.outcome === "setup-failure"
            ? "setup-failure"
            : "failed",
      detail: status,
    });
  }
  const blockers = rows.filter(({ state }) => !["passed", "docs-allowed-skip"].includes(state));
  return { rows, blockers, ok: blockers.length === 0 };
}

export function main(argv, io) {
  const [requiredPath, statusDir] = argv;
  if (!requiredPath || !statusDir) {
    io.stderr?.write("usage: audit-e2e-requirements.mjs <required-suites.json> <status-dir>\n");
    return 2;
  }
  if (!existsSync(requiredPath)) {
    io.stderr?.write(`required-suites file not found: ${requiredPath}\n`);
    return 2;
  }
  if (!existsSync(statusDir)) {
    io.stderr?.write(`status directory not found: ${statusDir}\n`);
    return 2;
  }
  const requiredSuites = JSON.parse(io.readFileSync(requiredPath, "utf8"));
  const audit = auditE2ERequirements({ requiredSuites, statusDir });
  const lines = ["| Suite | State |", "| --- | --- |"];
  for (const row of audit.rows.length > 0 ? audit.rows : audit.blockers)
    lines.push(
      `| ${row.suite} | ${row.state}${row.detail ? ` (${JSON.stringify(row.detail).slice(0, 200)})` : ""} |`,
    );
  if (audit.ok && audit.rows.length === 0)
    lines.push(
      "",
      `No browser suites required (${requiredSuites.classification}): ${requiredSuites.reason}`,
    );
  lines.push(
    "",
    audit.ok
      ? "Audit: all required suites accounted for."
      : "Audit: FAIL — required suite accounting is incomplete.",
  );
  io.stdout.write(`${lines.join("\n")}\n`);
  return audit.ok ? 0 : 1;
}

export function cli() {
  const io = {
    stdout: process.stdout,
    stderr: process.stderr,
    readFileSync,
  };
  return main(process.argv.slice(2), io);
}

if (
  process.argv[1] &&
  import.meta.url === (await import("node:url")).pathToFileURL(process.argv[1]).href
) {
  process.exit(cli());
}
