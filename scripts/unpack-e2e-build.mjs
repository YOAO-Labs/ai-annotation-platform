import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { open, readFile } from "node:fs/promises";

async function unpack(fingerprint, destination) {
  if (fingerprint?.length !== 64 || /[^0-9a-f]/.test(fingerprint) || !destination) {
    throw new Error("Usage: node unpack-e2e-build.mjs <build fingerprint> <destination>");
  }

  const archive = `web-e2e-dist-${fingerprint}.tar.gz`;
  const manifest = await readFile("SHA256SUMS", "utf8");
  const record = manifest.endsWith("\n") ? manifest.slice(0, -1) : manifest;
  const digest = record.slice(0, 64);
  if (
    !/^[0-9a-f]{64}$/i.test(digest) ||
    record[64] !== " " ||
    (record[65] !== " " && record[65] !== "*") ||
    record.slice(66) !== archive
  ) {
    throw new Error(`SHA256SUMS must contain exactly one valid record for ${archive}`);
  }

  const file = await open(
    archive,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    if (!(await file.stat()).isFile()) throw new Error(`${archive} is not a regular file`);
    const hash = createHash("sha256");
    for await (const chunk of file.createReadStream({ start: 0, autoClose: false })) {
      hash.update(chunk);
    }
    if (hash.digest("hex") !== digest.toLowerCase()) {
      throw new Error(`SHA-256 mismatch for ${archive}`);
    }
    execFileSync("tar", ["-xzf", "-", "-C", destination], {
      stdio: [file.fd, "inherit", "inherit"],
    });
  } finally {
    await file.close();
  }
}

try {
  await unpack(process.argv[2], process.argv[3]);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
