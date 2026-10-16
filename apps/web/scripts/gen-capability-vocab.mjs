#!/usr/bin/env node
// v0.18.30 · 从 capability-registry.snapshot.json 生成前端受控词表常量 (pnpm codegen 的一环)。
// 镜像 OpenAPI codegen: 读后端导出的 snapshot (不依赖运行后端) → 静态 ts 常量。
// 受控词表 SSOT 在后端 app/services/capability_registry.py; 生成物 gitignore, build 时重生成。

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const snapshotPath = resolve(webRoot, "../api/capability-registry.snapshot.json");
const outDir = resolve(webRoot, "src/api/generated");
const outPath = resolve(outDir, "capabilityVocab.gen.ts");

if (!existsSync(snapshotPath)) {
  console.error(`[gen-capability-vocab] snapshot 不存在: ${snapshotPath}`);
  console.error("先运行: cd apps/api && uv run python ../../scripts/export_capability_registry.py");
  process.exit(1);
}

const data = JSON.parse(readFileSync(snapshotPath, "utf-8"));
const s = (v) => JSON.stringify(v);

function idsBlock(typeName, constName, items) {
  return `export const ${constName} = [${items.map((i) => s(i.id)).join(", ")}] as const;
export type ${typeName} = (typeof ${constName})[number];`;
}

function labeledRecord(recordName, typeName, items) {
  const entries = items
    .map((i) => `  ${i.id}: { label: ${s(i.label)}, summary: ${s(i.summary)} },`)
    .join("\n");
  return `export const ${recordName}: Record<${typeName}, LabeledMeta> = {\n${entries}\n};`;
}

const { tasks, infras, modalities, geometries, prompts, inputs } = data;

const taskEntries = tasks
  .map(
    (t) =>
      `  ${t.id}: { label: ${s(t.label)}, summary: ${s(t.summary)}, defaultGeometry: ${s(
        t.default_geometry,
      )}, defaultModalities: ${s(t.default_modalities)} },`,
  )
  .join("\n");

const promptEntries = prompts
  .map(
    (p) =>
      `  ${p.id}: { label: ${s(p.label)}, summary: ${s(p.summary)}, requiresInput: ${
        p.requires_input
      }, interactiveRoute: ${p.interactive_route} },`,
  )
  .join("\n");

const interactiveRoute = prompts.filter((p) => p.interactive_route).map((p) => p.id);
const requiresInput = prompts.filter((p) => p.requires_input).map((p) => p.id);

const out = `// AUTO-GENERATED from apps/api/capability-registry.snapshot.json — do not edit.
// 由 apps/web/scripts/gen-capability-vocab.mjs 生成 (pnpm codegen)。
// 受控词表 SSOT 在后端 capability_registry; 改后端后重导 snapshot 即同步 (pre-commit 自动)。

${idsBlock("TaskId", "TASK_IDS", tasks)}
export const TASK_ALIASES: Readonly<Record<string, string>> = ${JSON.stringify(data.task_aliases ?? {}, null, 2)};
export const normalizeTask = (value: string): string =>
  Object.prototype.hasOwnProperty.call(TASK_ALIASES, value) ? TASK_ALIASES[value]! : value;
export interface TaskMeta {
  label: string;
  summary: string;
  defaultGeometry: string[];
  defaultModalities: string[];
}
export const TASKS: Record<TaskId, TaskMeta> = {
${taskEntries}
};

export interface LabeledMeta {
  label: string;
  summary: string;
}

${idsBlock("InfraId", "INFRA_IDS", infras)}
${labeledRecord("INFRAS", "InfraId", infras)}

${idsBlock("ModalityId", "MODALITY_IDS", modalities)}
${labeledRecord("MODALITIES", "ModalityId", modalities)}

${idsBlock("GeometryId", "GEOMETRY_IDS", geometries)}
${labeledRecord("GEOMETRIES", "GeometryId", geometries)}

${idsBlock("PromptId", "PROMPT_IDS", prompts)}
export interface PromptMeta {
  label: string;
  summary: string;
  requiresInput: boolean;
  interactiveRoute: boolean;
}
export const PROMPTS: Record<PromptId, PromptMeta> = {
${promptEntries}
};
// 派生集合: interactiveRoute = 进画布交互工具线 (不含 text); requiresInput = 需用户/上游输入 (含 text)。
export const INTERACTIVE_ROUTE_PROMPT_IDS = [${interactiveRoute.map(s).join(", ")}] as const;
export const REQUIRES_INPUT_PROMPT_IDS = [${requiresInput.map(s).join(", ")}] as const;

${idsBlock("InputId", "INPUT_IDS", inputs)}
${labeledRecord("INPUTS", "InputId", inputs)}
`;

mkdirSync(outDir, { recursive: true });
writeFileSync(outPath, out, "utf-8");
console.log(`[gen-capability-vocab] wrote ${outPath}`);
