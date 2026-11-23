#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");

const PAIRS = [
  {
    snapshot: resolve(webRoot, "../api/openapi.snapshot.json"),
    generated: resolve(webRoot, "src/api/generated/types.gen.ts"),
  },
  {
    snapshot: resolve(webRoot, "../api/capability-registry.snapshot.json"),
    generated: resolve(webRoot, "src/api/generated/capabilityVocab.gen.ts"),
  },
];

function runCodegen() {
  const result = spawnSync("pnpm", ["codegen"], {
    cwd: webRoot,
    stdio: "inherit",
    shell: true,
  });
  process.exit(result.status ?? 1);
}

if (process.env.OPENAPI_URL) {
  console.log("[codegen-if-changed] OPENAPI_URL set, regenerating");
  runCodegen();
}

for (const { generated } of PAIRS) {
  if (!existsSync(generated)) {
    console.log(`[codegen-if-changed] missing ${generated}, regenerating`);
    runCodegen();
  }
}

let needs = false;
for (const { snapshot, generated } of PAIRS) {
  if (!existsSync(snapshot)) {
    console.warn(`[codegen-if-changed] snapshot not found at ${snapshot} — skipping that pair`);
    continue;
  }
  if (statSync(snapshot).mtimeMs > statSync(generated).mtimeMs) {
    console.log(`[codegen-if-changed] ${snapshot} newer than generated, regenerating`);
    needs = true;
  }
}

if (needs) {
  runCodegen();
} else {
  console.log("[codegen-if-changed] snapshots unchanged, skipping codegen");
  process.exit(0);
}
