#!/usr/bin/env node
// 从 apps/web/src/pages/Workbench/state/hotkeys.ts 中的 HOTKEYS 数组 + HOTKEY_CATEGORY_LABEL
// 生成 docs-site/user-guide/workbench/hotkeys.generated.md。
// 在 docs:dev / docs:build 之前自动执行。
//
// 使用项目已有 TypeScript parser 读取字面量，校验命令 ID 与元数据；不执行源码。
// 解析失败会报错退出，让漂移问题暴露在 CI 而不是文档站静默错乱。

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { emitGenerated } from "./_emit.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../apps/web/src/pages/Workbench/state/hotkeys.ts");
const dst = resolve(here, "../user-guide/workbench/hotkeys.generated.md");

const text = readFileSync(src, "utf8");

// 2. Read complete records, including id / category / stages / applies metadata.
const ts = createRequire(src)("typescript");
const sourceFile = ts.createSourceFile(src, text, ts.ScriptTarget.Latest, true);
const declaration = sourceFile.statements
  .filter(ts.isVariableStatement)
  .flatMap((statement) => [...statement.declarationList.declarations])
  .find((node) => node.name.getText(sourceFile) === "HOTKEYS");
if (!declaration?.initializer || !ts.isArrayLiteralExpression(declaration.initializer)) {
  throw new Error("HOTKEYS must be an array literal");
}
const contexts = new Set([
  "image",
  "video",
  "image-prediction",
  "video-prediction",
  "video-no-prediction",
]);
const categories = new Set([
  "common",
  "draw",
  "selection",
  "canvas",
  "ai",
  "playback",
  "task",
  "mouse",
]);
const stages = new Set(["image", "video", "threed"]);
const itemIds = new Set();
const items = declaration.initializer.elements.map((node) => {
  if (!ts.isObjectLiteralExpression(node)) throw new Error("Hotkey must be an object literal");
  const item = {};
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property)) throw new Error("Hotkey properties must be literals");
    const key = property.name.getText(sourceFile);
    const value = property.initializer;
    if (ts.isStringLiteral(value)) item[key] = value.text;
    else if (value.kind === ts.SyntaxKind.TrueKeyword) item[key] = true;
    else if (value.kind === ts.SyntaxKind.FalseKeyword) item[key] = false;
    else if (
      key === "keys" &&
      ts.isArrayLiteralExpression(value) &&
      value.elements.every(ts.isStringLiteral)
    ) {
      item.keys = value.elements.map((element) => element.text);
    } else if (key === "stages" && ts.isArrayLiteralExpression(value)) {
      item.stages = value.elements.map((element) => {
        if (!ts.isStringLiteral(element)) throw new Error("stages must be string literals");
        return element.text;
      });
    } else throw new Error(`Unsupported hotkey property: ${key}`);
  }
  if (!item.id) throw new Error("Hotkey definition requires a stable id");
  if (!item.desc || !item.category) throw new Error("Incomplete hotkey definition");
  if (!item.keys?.length && !item.keysLabel) throw new Error("Incomplete hotkey definition");
  if (itemIds.has(item.id)) throw new Error(`Duplicate hotkey id: ${item.id}`);
  itemIds.add(item.id);
  if (item.category && !categories.has(item.category))
    throw new Error(`Unknown hotkey category: ${item.category}`);
  if (item.stages?.some((s) => !stages.has(s))) {
    throw new Error(`Unknown hotkey stage in ${item.id}`);
  }
  if (item.context && !contexts.has(item.context))
    throw new Error(`Unknown hotkey context: ${item.context}`);
  if (item.targetTool && !["setTool", "setVideoTool"].includes(item.actionType))
    throw new Error("Tool target requires a tool action");
  return item;
});
if (items.length === 0) throw new Error("HOTKEYS is empty");

// 3. 找 HOTKEY_CATEGORY_LABEL / HOTKEY_STAGE_LABEL
const categoryLabel = readLabelMap("HOTKEY_CATEGORY_LABEL");
const stageLabel = readLabelMap("HOTKEY_STAGE_LABEL");

function readLabelMap(name) {
  const re = new RegExp(`export const ${name}[^=]*=\\s*\\{([^}]+)\\}`);
  const match = text.match(re);
  const map = {};
  if (match) {
    for (const g of match[1].matchAll(/(\w+):\s*"((?:[^"\\]|\\.)*)"/g)) {
      map[g[1]] = g[2].replace(/\\"/g, '"');
    }
  }
  return map;
}

// 4. 按 category 输出
const categoryOrder = ["draw", "selection", "canvas", "ai", "playback", "task", "mouse", "threed"];
const byCategory = new Map();
for (const it of items) {
  // common 是精选视图：条目按其 home 分类归档，常用行另外汇总到开头。
  const home = it.category === "common" ? "common" : it.category;
  if (!byCategory.has(home)) byCategory.set(home, []);
  byCategory.get(home).push(it);
}
const commonRows = items.filter((it) => it.common);

const lines = [];
lines.push("<!-- AUTO-GENERATED — 由 docs-site/scripts/generate-hotkeys.mjs 从 -->");
lines.push("<!-- apps/web/src/pages/Workbench/state/hotkeys.ts 生成。请勿手改。 -->");
lines.push("");
lines.push(
  "同一按键按当前工作台与选择上下文路由。输入框、菜单、停靠标签、类别弹层及活跃工具自有按键优先；A / D 仅处理普通待决候选，成功后才自动前进，视频限定当前帧。交互式 SAM 使用 Enter / Esc / Tab，追踪作业使用其审阅操作。",
);
lines.push("");
lines.push(
  "类别直选是数字 1-9 加 0（共十个槽位，按项目类别配置顺序取前十个）；其余类别用类别面板搜索或点击。图片属性数字键只在选中卡「属性快捷键」区域聚焦后生效。账号可在工作台快捷键面板修改可编辑命令（未标注「固定」）的组合与停用状态，改动会参与冲突检查；标注「固定」的快捷键由对应功能固定持有。",
);
lines.push("");

const stageText = (stagesList) => {
  if (!stagesList || stagesList.length >= 3) return "全部工作台";
  return stagesList.map((s) => stageLabel[s] ?? s).join(" / ");
};

const formatKey = (k) => `\`${k}\``;
const formatKeys = (keys) => keys.map(formatKey).join(" + ");

const rowLine = (it) => {
  const keys = it.keysLabel ?? formatKeys(it.keys ?? []);
  const appliesParts = [];
  if (it.stages) appliesParts.push(stageText(it.stages));
  if (it.applies) appliesParts.push(it.applies);
  const applies = appliesParts.length > 0 ? appliesParts.join("；") : "—";
  return `| ${keys} | ${it.desc} | ${applies} |`;
};

const table = (list) => {
  lines.push("| 快捷键 | 动作 | 生效范围 / 时机 |");
  lines.push("|---|---|---|");
  for (const it of list) lines.push(rowLine(it));
  lines.push("");
};

if (commonRows.length > 0) {
  lines.push(`### ${categoryLabel.common ?? "常用"}（精选）`);
  lines.push("");
  table(commonRows);
}

for (const g of categoryOrder) {
  if (g === "common") continue;
  const list = byCategory.get(g);
  if (!list) continue;
  lines.push(`### ${categoryLabel[g] ?? g}`);
  lines.push("");
  table(list);
}
const rest = items.filter((it) => !it.common && !categoryOrder.includes(it.category));
if (rest.length > 0) {
  lines.push("### 其他");
  lines.push("");
  table(rest);
}

emitGenerated({
  dst,
  content: lines.join("\n"),
  label: "generate-hotkeys",
  detail: `${items.length} hotkeys`,
});
