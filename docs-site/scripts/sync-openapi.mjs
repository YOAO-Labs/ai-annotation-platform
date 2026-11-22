#!/usr/bin/env node
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../apps/api/openapi.snapshot.json");
const dst = resolve(here, "../public/openapi.json");

if (!existsSync(src)) {
  console.error(
    `[sync-openapi] snapshot 不存在：${src}\n` +
      `先运行：cd apps/api && uv run python ../../scripts/export_openapi.py`,
  );
  process.exit(1);
}

mkdirSync(dirname(dst), { recursive: true });
copyFileSync(src, dst);
console.log(`[sync-openapi] copied → ${dst}`);
