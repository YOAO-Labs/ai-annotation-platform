import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

const runner = fileURLToPath(new URL("./run-ml-cpu-tests.sh", import.meta.url));
const registry = JSON.parse(readFileSync(new URL("./test-suites.json", import.meta.url), "utf8"));
const run = (...args) => spawnSync("bash", [runner, ...args], { encoding: "utf8" });

test("all registered suites reach the CI matrix with their interpreter", () => {
  const result = run("plan", "all");
  assert.equal(result.status, 0, result.stderr);
  const matrix = JSON.parse(result.stdout.split("\n").find((line) => line.startsWith("matrix=")).slice(7));
  assert.deepEqual(matrix.include, registry.ml_cpu.map(({ id, python }) => ({ suite: id, python })));
});

test("selection and failures do not silently select another suite", () => {
  const result = run("plan", "libreyolo,inference-plugin-sdk");
  assert.equal(result.status, 0, result.stderr);
  const matrix = JSON.parse(result.stdout.split("\n").find((line) => line.startsWith("matrix=")).slice(7));
  assert.deepEqual(matrix.include.map(({ suite }) => suite), ["libreyolo", "inference-plugin-sdk"]);
  for (const spec of ["", "libreyolo,missing", "*"]) {
    const bad = run("plan", spec);
    assert.notEqual(bad.status, 0);
    assert.doesNotMatch(bad.stdout, /^matrix=/m);
  }
  assert.notEqual(run("run", "missing").status, 0);
});
