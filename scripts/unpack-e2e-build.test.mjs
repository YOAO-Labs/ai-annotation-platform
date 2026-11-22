import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const cli = fileURLToPath(new URL("./unpack-e2e-build.mjs", import.meta.url));
const fingerprint = "a".repeat(64);
const basename = `web-e2e-dist-${fingerprint}.tar.gz`;

function buildFixture(t) {
  const cwd = mkdtempSync(join(tmpdir(), "e2e-build-"));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const destination = join(cwd, "web");
  mkdirSync(destination);
  mkdirSync(join(cwd, "dist"));
  writeFileSync(join(cwd, "dist", "index.html"), "shared build");
  execFileSync("tar", ["-czf", basename, "dist"], { cwd });
  const archive = join(cwd, basename);
  const digest = createHash("sha256").update(readFileSync(archive)).digest("hex");
  const record = `${digest}  ${basename}\n`;
  const run = (manifest, requestedFingerprint = fingerprint) => {
    writeFileSync(join(cwd, "SHA256SUMS"), manifest);
    const result = spawnSync(process.execPath, [cli, requestedFingerprint, destination], {
      cwd,
      encoding: "utf8",
    });
    assert.ifError(result.error);
    return result;
  };
  return { cwd, archive, destination, digest, record, run };
}

test("a single exact checksum record verifies and extracts the shared build", (t) => {
  const { destination, record, run } = buildFixture(t);
  const result = run(record);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(readFileSync(join(destination, "dist", "index.html"), "utf8"), "shared build");
});

for (const [name, entry] of [
  ["wrong basename", "different.tar.gz"],
  ["expected basename as a prefix", `${basename}.backup`],
  ["expected basename as a suffix", `backup-${basename}`],
  ["path-qualified basename", `nested/${basename}`],
  ["dot-prefixed path", `./${basename}`],
]) {
  test(`rejects ${name} before extracting`, (t) => {
    const { cwd, archive, destination, digest, run } = buildFixture(t);
    if (entry.startsWith("nested/")) mkdirSync(join(cwd, "nested"));
    if (entry !== `./${basename}`) writeFileSync(join(cwd, entry), readFileSync(archive));
    const result = run(`${digest}  ${entry}\n`);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /exactly one valid record/);
    assert.equal(existsSync(join(destination, "dist")), false);
  });
}

for (const [name, manifest] of [
  ["duplicate records", ({ record }) => record.repeat(2)],
  ["trailing blank record", ({ record }) => `${record}\n`],
  ["invalid digest", ({ record }) => `g${record.slice(1)}`],
  ["invalid separator", ({ digest }) => `${digest} ${basename}\n`],
]) {
  test(`rejects ${name} before extracting`, (t) => {
    const fixture = buildFixture(t);
    const result = fixture.run(manifest(fixture));
    assert.equal(result.status, 1);
    assert.match(result.stderr, /exactly one valid record/);
    assert.equal(existsSync(join(fixture.destination, "dist")), false);
  });
}

for (const [name, change, error] of [
  ["tampered archive", (archive) => appendFileSync(archive, "tampered"), /SHA-256 mismatch/],
  ["missing archive", (archive) => rmSync(archive), /ENOENT/],
  [
    "symlink archive",
    (archive) => {
      renameSync(archive, `${archive}.original`);
      symlinkSync(`${archive}.original`, archive);
    },
    /ELOOP/,
  ],
]) {
  test(`rejects ${name} before extracting`, (t) => {
    const { archive, destination, record, run } = buildFixture(t);
    change(archive);
    const result = run(record);
    assert.equal(result.status, 1);
    assert.match(result.stderr, error);
    assert.equal(existsSync(join(destination, "dist")), false);
  });
}

test("rejects a path-like fingerprint before extracting", (t) => {
  const { destination, record, run } = buildFixture(t);
  const result = run(record, `../${fingerprint.slice(3)}`);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Usage:/);
  assert.equal(existsSync(join(destination, "dist")), false);
});
