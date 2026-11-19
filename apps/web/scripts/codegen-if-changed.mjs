#!/usr/bin/env node
// 仅在 snapshot 比生成产物新（或缺产物）时跑 codegen，加速 `pnpm build`。
// 扩展为多 snapshot 检查: OpenAPI 类型 + capability registry 受控词表。
//
// 触发重新生成的条件（任一）：
//   1. 任一生成产物 (types.gen.ts / capabilityVocab.gen.ts) 不存在
//   2. 任一 snapshot 的 mtime 比其对应生成产物新
//   3. OPENAPI_URL 环境变量被显式设置（CI / 自定义场景，绕过 mtime 比较）
//
// 强制重新生成：删除 src/api/generated 后跑 `pnpm build`，或直接 `pnpm codegen`。

import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");

// 每条 = 一对 (snapshot 源, 生成产物)。pnpm codegen 一次跑全部生成步骤,
// 故任一对需要重生成就整体跑一次即可。
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
  process.exit(result.status ?? 0);
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
