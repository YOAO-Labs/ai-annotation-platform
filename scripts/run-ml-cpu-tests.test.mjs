import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const runner = fileURLToPath(new URL("./run-ml-cpu-tests.sh", import.meta.url));
const run = (...args) => spawnSync("bash", [runner, ...args], { encoding: "utf8" });

test("selection and failures do not silently select another suite", () => {
  const result = run("plan", "libreyolo,inference-plugin-sdk");
  assert.equal(result.status, 0, result.stderr);
  const matrix = JSON.parse(
    result.stdout
      .split("\n")
      .find((line) => line.startsWith("matrix="))
      .slice(7),
  );
  assert.deepEqual(
    matrix.include.map(({ suite }) => suite),
    ["libreyolo", "inference-plugin-sdk"],
  );
  for (const spec of ["", "libreyolo,missing", "*"]) {
    const bad = run("plan", spec);
    assert.notEqual(bad.status, 0);
    assert.doesNotMatch(bad.stdout, /^matrix=/m);
  }
  assert.notEqual(run("run", "missing").status, 0);
});
