#!/usr/bin/env node
/**
 * Fail-closed audit of planned E2E suites against actual result artifacts.
 *
 * Inputs:
 * - required suites JSON: array of { suite, planned: true, docsAllowedSkip? }
 *   (the planner shadow/legacy list, one entry per suite the run promised)
 * - a directory of per-suite completion artifacts written by the runner:
 *   <dir>/<suite>.json with { outcome, stats?, timings? } where outcome is
 *   "success" | "failure" | "timedout" | "cancelled" | "setup-failure".
 *
 * Verdict (exit code) is fail-closed: a required suite with a missing,
 * cancelled, setup-failure or failed artifact fails the audit. Explicit
 * docs-allowed skips (planned=false with a documented reason) never fail.
 * Reusable now as an advisory audit next to the frozen legacy gate; P9 turns
 * it into the required gate when the §6 selection switches.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Validate the required-suites manifest.
 *
 * Shape: either a plain array of suite entries, or
 * { classification: "docs-only" | <other>, reason: string, suites: [...] }.
 * A docs-allowed skip (`planned: false` + `docsAllowedSkip: true` + reason) is
 * only legal inside a docs-only classification, so a runner flag can never
 * bypass a required suite. Entries with `planned: true` are always required.
 */
export function validateRequiredManifest(manifest) {
  const errors = [];
  // Normalize BOTH shapes (array = legacy frozen gate, object = §6 manifest)
  // into one internal form, then run the SAME validation over every entry.
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
    if (rawSuites.length === 0 && classification === "docs-only" && !String(reason).trim())
      errors.push("docs-only manifest with an empty required list requires a nonblank reason");
    if (classification === "docs-only" && !String(reason).trim())
      errors.push("docs-only manifest requires a nonblank reason");
  } else {
    return {
      classification: "invalid",
      reason: "",
      suites: [],
      errors: ["manifest must be a suite array or {classification, reason, suites}"],
    };
  }

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
  const requiredSuites = manifest.suites;
  if (requiredSuites.length === 0 && manifest.classification !== "docs-only")
    return {
      rows: [],
      blockers: [
        {
          suite: "(manifest)",
          state: "invalid-manifest",
          detail: "empty required list without a docs-only classification",
        },
      ],
      ok: false,
    };
  {
    const rows = [];
    const missing = [];
    if (requiredSuites.length === 0 && manifest.classification !== "docs-only")
      return {
        rows: [],
        blockers: [
          {
            suite: "(manifest)",
            state: "invalid-manifest",
            detail: "empty required list without a docs-only classification",
          },
        ],
        ok: false,
      };
    for (const entry of requiredSuites) {
      const suite = entry.suite;
      const docsAllowedSkip = entry.docsAllowedSkip === true && entry.planned === false;
      const file = join(statusDir, `${suite}.json`);
      if (!existsSync(file)) {
        missing.push(suite);
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
      // Artifact identity: the completion artifact must name its own suite,
      // carry a known outcome, and back a success with numeric stats (a bare
      // {outcome:"success"} without evidence is rejected fail-closed).
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
          (key) => typeof stats[key] === "number",
        );
        if (!numeric) {
          rows.push({ suite, state: "malformed-success", detail: "success without numeric stats" });
          continue;
        }
        if (entry.flakyPolicy === "forbid" && status.stats.flaky > 0) {
          rows.push({ suite, state: "core-flaky", detail: { flaky: status.stats.flaky } });
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
    const blockingStates = [
      "missing",
      "failed",
      "cancelled",
      "setup-failure",
      "skipped-not-run",
      "unreadable",
      "suite-mismatch",
      "unknown-outcome",
      "malformed-success",
      "core-flaky",
    ];
    const blockers = rows.filter((row) => blockingStates.includes(row.state));
    return { rows, blockers, ok: blockers.length === 0 };
  }
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
  const statusFiles = existsSync(statusDir)
    ? readdirSync(statusDir).filter((name) => name.endsWith(".json"))
    : [];
  if (requiredSuites.length > 0 && statusFiles.length === 0) {
    io.stderr.write("no suite status artifacts found for a non-empty required list\n");
    return 1;
  }
  const audit = auditE2ERequirements({ requiredSuites, statusDir });
  const lines = ["| Suite | State |", "| --- | --- |"];
  for (const row of audit.rows)
    lines.push(
      `| ${row.suite} | ${row.state}${row.detail ? ` (${JSON.stringify(row.detail).slice(0, 200)})` : ""} |`,
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
    existsSync,
    readFileSync,
    readdirSync,
  };
  return main(process.argv.slice(2), io);
}

if (
  process.argv[1] &&
  import.meta.url === (await import("node:url")).pathToFileURL(process.argv[1]).href
) {
  process.exit(cli());
}
