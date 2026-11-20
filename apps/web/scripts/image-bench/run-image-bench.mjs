#!/usr/bin/env node
// image-bench orchestrator（镜像 scripts/video-bench/run-video-bench.mjs）。
//
// 当前作用：构建 size × density 矩阵，写 manifest.json 到 test-results/image-bench/{runId}/。
// 真正的 Playwright trace 捕获待 _test_seed 加 ?image_size=&annotation_density= 参数 + 真实
// 图片素材（PIL 生成 / git-lfs）落地后接入。spec 已就位：
//   apps/web/e2e/tests/image-bench-fixtures.spec.ts
// 通过环境变量 IMAGE_BENCH_SIZE / IMAGE_BENCH_DENSITY 选场景，page.evaluate 读
// window.__workbenchPerf 输出基准 JSON。
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "../../..");
const defaultOutDir = resolve(repoRoot, "test-results/image-bench");

function parseArgs(argv) {
  const args = {
    dryRun: false,
    outDir: defaultOutDir,
    baseUrl: process.env.IMAGE_BENCH_BASE_URL ?? "http://localhost:3000",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--") continue;
    else if (arg === "--dry-run") args.dryRun = true;
    else if (arg === "--out") {
      args.outDir = resolve(argv[i + 1] ?? defaultOutDir);
      i += 1;
    } else if (arg === "--base-url") {
      args.baseUrl = argv[i + 1] ?? args.baseUrl;
      i += 1;
    } else throw new Error(`Unknown argument: ${arg}`);
  }
  return args;
}

function buildMatrix(config) {
  const matrix = [];
  for (const fx of config.fixtures) {
    for (const den of config.annotationDensities) {
      matrix.push({
        id: `${fx.id}-${den.id}`,
        fixtureId: fx.id,
        densityId: den.id,
        label: `${fx.label} / ${den.count} shapes`,
        imageSize: fx.imageSize,
        widthPx: fx.widthPx,
        heightPx: fx.heightPx,
        shapeCount: den.count,
        scenarios: config.scenarios,
      });
    }
  }
  return matrix;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const configPath = resolve(__dirname, "fixtures.json");
  const config = JSON.parse(await readFile(configPath, "utf8"));
  const matrix = buildMatrix(config);
  const runId = new Date().toISOString().replace(/[:.]/g, "-");
  const manifest = {
    runId,
    generatedAt: new Date().toISOString(),
    baseUrl: args.baseUrl,
    fixtureConfig: "apps/web/scripts/image-bench/fixtures.json",
    summaryOutput: `test-results/image-bench/${runId}/summary.json`,
    budgets: config.budgets,
    matrix,
  };

  console.log(`image-bench matrix: ${matrix.length} runs`);
  for (const item of matrix) console.log(`- ${item.id}: ${item.label}`);

  if (args.dryRun) {
    console.log("dry-run: no files written");
    return;
  }

  const runDir = resolve(args.outDir, runId);
  await mkdir(runDir, { recursive: true });
  await writeFile(
    resolve(runDir, "manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  await writeFile(
    resolve(runDir, "README.md"),
    [
      "# Image Bench Run",
      "",
      `- Run ID: \`${runId}\``,
      `- Base URL: \`${args.baseUrl}\``,
      `- Matrix: ${matrix.length} runs`,
      "",
      "本目录由 `pnpm --filter web image:bench` 写入，记录 fixture 矩阵和基准配置。",
      "真实 trace 捕获待 _test_seed router 加 ?image_size= / ?annotation_density= 参数 + 测试图片资源落地",
      "（PIL 程序生成 2K/8K 渐变 + polygon-dense 标注 / 或 git-lfs 入仓）。",
      "",
      "矩阵参考 `apps/web/e2e/tests/image-bench-fixtures.spec.ts` 走 env vars 跑单场景：",
      "`IMAGE_BENCH_SIZE=2k IMAGE_BENCH_DENSITY=10 pnpm --filter web test:e2e image-bench-fixtures`。",
      "",
    ].join("\n"),
    "utf8",
  );
  console.log(`wrote ${resolve(runDir, "manifest.json")}`);
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
