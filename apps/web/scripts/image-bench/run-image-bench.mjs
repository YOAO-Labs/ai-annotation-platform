#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "../../../..");
const defaultOutDir = resolve(repoRoot, "test-results/image-bench");

function parseArgs(argv) {
  const args = {
    dryRun: false,
    outDir: defaultOutDir,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--") continue;
    else if (arg === "--dry-run") args.dryRun = true;
    else if (arg === "--out") {
      args.outDir = resolve(argv[i + 1] ?? defaultOutDir);
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
        imageCanvas: fx.imageCanvas,
        widthPx: fx.widthPx,
        heightPx: fx.heightPx,
        annotationCount: den.count,
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
  const planId = new Date().toISOString().replace(/[:.]/g, "-");
  const manifest = {
    kind: "plan",
    planId,
    generatedAt: new Date().toISOString(),
    fixtureConfigVersion: config.version,
    actions: config.actions,
    budgets: config.budgets,
    matrix,
  };

  console.log(`image-bench plan: ${matrix.length} scenarios, none executed`);
  for (const item of matrix) {
    console.log(`- ${item.id}: ${item.widthPx}×${item.heightPx}, ${item.annotationCount} boxes`);
  }

  if (args.dryRun) {
    console.log("dry-run: no files written");
    return;
  }

  const runDir = resolve(args.outDir, planId);
  await mkdir(runDir, { recursive: true });
  await writeFile(
    resolve(runDir, "manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  console.log(`wrote ${resolve(runDir, "manifest.json")}`);
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
