import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

function select(pages) {
  return execFileSync(
    "jq",
    ["-r", "-f", fileURLToPath(new URL("./select-e2e-report-artifacts.jq", import.meta.url))],
    { input: JSON.stringify(pages), encoding: "utf8" },
  ).trim();
}

test("reruns select the latest report by creation time and retain other suites", () => {
  assert.equal(
    select([
      {
        artifacts: [
          { name: "playwright-report-mask", id: 99, created_at: "2026-10-15T03:02:22Z" },
          { name: "playwright-report-smoke", id: 80, created_at: "2026-10-15T03:03:00Z" },
          { name: "backend-test-results", id: 100, created_at: "2026-10-15T03:04:00Z" },
        ],
      },
      {
        artifacts: [{ name: "playwright-report-mask", id: 42, created_at: "2026-10-15T03:25:22Z" }],
      },
    ]),
    "42,80",
  );
});

test("a newer failed report supersedes an earlier successful report", () => {
  assert.equal(
    select([
      {
        artifacts: [
          { name: "playwright-report-mask", id: 11, created_at: "2026-10-15T03:25:22Z" },
          { name: "playwright-report-mask", id: 12, created_at: "2026-10-15T03:02:22Z" },
        ],
      },
    ]),
    "11",
  );
});

test("missing reports produce no IDs for the audit to reject", () => {
  assert.equal(select([{ artifacts: [] }]), "");
});
