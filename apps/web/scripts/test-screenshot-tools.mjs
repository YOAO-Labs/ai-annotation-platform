import { spawnSync } from "node:child_process";
import { globSync } from "node:fs";

const pattern = "e2e/screenshots/**/*.test.{mjs,ts}";
const files = globSync(pattern).sort();
// Node 22 exits successfully for `node --test` with an unmatched glob.
if (!files.length) throw new Error(`No screenshot tool tests matched ${pattern}`);
console.log(files.join("\n"));

// The encoder fixtures use real 4K video. Run one file at a time on CI runners.
const result = spawnSync(
  process.execPath,
  ["--test", "--test-concurrency=1", ...process.argv.slice(2), ...files],
  { stdio: "inherit" },
);
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
