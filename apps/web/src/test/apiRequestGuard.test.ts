import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { expect, it } from "vitest";

interface CaseResult {
  title: string;
  status: string;
  failureMessages: string[];
}

interface HarnessResult {
  numFailedTests: number;
  numPassedTests: number;
  testResults: { message: string; assertionResults: CaseResult[] }[];
}

it("blocks unmatched product requests and fails only their owning Vitest cases", ({ task }) => {
  const dir = mkdtempSync(join(tmpdir(), "web-msw-guard-"));
  try {
    const report = join(dir, "vitest.json");
    const cli = createRequire(task.file.filepath).resolve("vitest/vitest.mjs");
    const result = spawnSync(
      process.execPath,
      [
        cli,
        "run",
        "--config",
        "src/test/fixtures/apiRequestGuard.config.mts",
        "--maxWorkers=1",
        "--minWorkers=1",
        "--reporter=json",
        `--outputFile=${report}`,
      ],
      {
        cwd: resolve(dirname(task.file.filepath), "../.."),
        encoding: "utf8",
        timeout: 25_000,
      },
    );
    expect(result.error).toBeUndefined();
    expect(result.status, `${result.stdout}\n${result.stderr}`).toBe(1);

    const run: HarnessResult = JSON.parse(readFileSync(report, "utf8"));
    expect(run.testResults).toHaveLength(1);
    const [file] = run.testResults;
    expect(file.message).toBe("");
    expect(run.numFailedTests).toBe(5);
    expect(run.numPassedTests).toBe(5);
    const byTitle = new Map(file.assertionResults.map((test) => [test.title, test]));
    for (const [title, request] of [
      ["fails the case that caught an unmatched fetch", "GET /api/v7?detail=missing"],
      ["fails if a previous case's handler is no longer in scope", "GET /api/v7/scoped"],
      ["owns a request dispatched during the next case", "GET /api/v7/late"],
      ["restores interception after a fetch double", "GET /api/v7/after-double"],
      ["restores interception after a fetch spy", "GET /api/v7/after-spy"],
    ]) {
      expect(byTitle.get(title)).toMatchObject({
        status: "failed",
        failureMessages: [`未声明 MSW handler 的产品 API 请求：\n${request}`],
      });
    }
    for (const title of [
      "passes the next case with an explicit handler",
      "does not inherit an earlier case's late failure",
      "accepts an explicit fetch double",
      "accepts an explicit fetch spy",
      "allows a real nonproduct path even with an API path in its query",
    ]) {
      expect(byTitle.get(title)).toMatchObject({ status: "passed", failureMessages: [] });
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}, 30_000);
