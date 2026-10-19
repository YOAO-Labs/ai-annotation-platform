import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

const vector = JSON.parse(
  await readFile(new URL("./fixtures/canonical-rle.json", import.meta.url), "utf8"),
);
const canonical = JSON.stringify({
  encoding: vector.rle.encoding,
  size: vector.rle.size,
  counts: vector.rle.counts,
});
const digest = createHash("sha256").update(canonical, "utf8").digest("hex");

assert.equal(canonical, vector.canonical_utf8);
assert.equal(digest, vector.sha256);
assert.equal(
  `raster-masks/sha256/${digest.slice(0, 2)}/${digest.slice(2, 4)}/${digest}.json`,
  vector.object_key,
);
