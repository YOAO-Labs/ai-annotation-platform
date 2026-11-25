import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

test("explicit Markdown paths check every selected file and ignore unrelated failures", (t) => {
  const repo = mkdtempSync(join(tmpdir(), "docs-link-selection-"));
  t.after(() => rmSync(repo, { recursive: true, force: true }));
  mkdirSync(join(repo, "scripts"));
  const script = join(repo, "scripts/check-dead-links.mjs");
  copyFileSync(new URL("./check-dead-links.mjs", import.meta.url), script);
  writeFileSync(join(repo, "README.md"), "[Guide](guide.md)\n");
  writeFileSync(join(repo, "guide.md"), "[Missing](gone.md)\n");
  writeFileSync(join(repo, "other.md"), "[Unrelated](old.md)\n");
  const check = (...paths) =>
    spawnSync(process.execPath, [script, "--json", "--", ...paths], { encoding: "utf8" });
  const selected = check("README.md", "guide.md");
  assert.equal(selected.status, 1, selected.stderr);
  assert.ok(selected.stdout, selected.stderr);
  assert.deepEqual(
    JSON.parse(selected.stdout).map(({ file, target }) => [file, target]),
    [["guide.md", "gone.md"]],
  );
  const clean = check("README.md");
  assert.equal(clean.status, 0, clean.stderr);
  assert.deepEqual(JSON.parse(clean.stdout), []);
});
