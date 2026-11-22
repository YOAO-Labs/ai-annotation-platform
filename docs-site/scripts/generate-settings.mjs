#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { emitGenerated } from "./_emit.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fieldsSrc = resolve(
  here,
  "../../apps/web/src/pages/Workbench/state/workbenchSettingsFields.ts",
);
const localSrc = resolve(
  here,
  "../../apps/web/src/pages/Workbench/state/workbenchLocalDefaults.ts",
);
const authSrc = resolve(here, "../../apps/web/src/api/auth.ts");
const dst = resolve(here, "../user-guide/workbench/settings.generated.md");
const ts = createRequire(fieldsSrc)("typescript");

function readDeclaration(text, name) {
  const source = ts.createSourceFile(`${name}.ts`, text, ts.ScriptTarget.Latest);
  const initializer = source.statements
    .filter(ts.isVariableStatement)
    .flatMap((statement) => [...statement.declarationList.declarations])
    .find((item) => ts.isIdentifier(item.name) && item.name.text === name)?.initializer;
  return initializer && ts.isAsExpression(initializer) ? initializer.expression : initializer;
}

function readProperties(node, name) {
  if (!node || !ts.isObjectLiteralExpression(node)) {
    throw new Error(`[generate-settings] ${name} 必须使用对象字面量`);
  }
  const entries = new Map();
  for (const property of node.properties) {
    if (
      !ts.isPropertyAssignment(property) ||
      !(ts.isIdentifier(property.name) || ts.isStringLiteral(property.name))
    ) {
      throw new Error(`[generate-settings] ${name} 包含未知定义`);
    }
    const key = property.name.text;
    if (entries.has(key)) throw new Error(`[generate-settings] ${name}.${key} 重复定义`);
    entries.set(key, property.initializer);
  }
  return entries;
}

function scalar(node) {
  if (!node) return undefined;
  if (ts.isStringLiteral(node)) return node.text;
  if (ts.isNumericLiteral(node)) return Number(node.text);
  if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
  return undefined;
}

function readFields(fieldsText) {
  const name = "WORKBENCH_SETTING_FIELDS";
  const array = readDeclaration(fieldsText, name);
  if (!array || !ts.isArrayLiteralExpression(array) || array.elements.length === 0) {
    throw new Error(`[generate-settings] ${name} 必须使用非空数组字面量`);
  }
  const keys = new Set();
  return array.elements.map((node, index) => {
    const field = readProperties(node, `${name}[${index}]`);
    const key = scalar(field.get("key"));
    const category = scalar(field.get("category"));
    const section = scalar(field.get("section"));
    const label = scalar(field.get("label"));
    const hidden = scalar(field.get("hidden"));
    const storage = scalar(field.get("storage"));
    if (
      typeof key !== "string" ||
      typeof category !== "string" ||
      !key.startsWith(`${category}.`) ||
      typeof section !== "string" ||
      typeof label !== "string" ||
      (field.has("hidden") && typeof hidden !== "boolean") ||
      (field.has("storage") && storage !== "local" && storage !== "preferences") ||
      (category === "experiment") !== (storage === "local")
    ) {
      throw new Error(`[generate-settings] ${key ?? `${name}[${index}]`} 缺少有效字段定义`);
    }
    if (keys.has(key)) throw new Error(`[generate-settings] ${key} 重复定义`);
    keys.add(key);
    const control = readProperties(field.get("control"), `${key}.control`);
    const ctrlType = scalar(control.get("type"));
    if (
      !["toggle", "select", "slider", "text", "multiselect", "labelContentByType"].includes(
        ctrlType,
      )
    ) {
      throw new Error(`[generate-settings] ${key} 包含未知控件`);
    }
    let options = [];
    if (ctrlType === "select") {
      const array = control.get("options");
      if (!array || !ts.isArrayLiteralExpression(array)) {
        throw new Error(`[generate-settings] ${key} 缺少选项定义`);
      }
      options = array.elements.map((option) => {
        const properties = readProperties(option, `${key}.options`);
        const value = scalar(properties.get("value"));
        const label = scalar(properties.get("label"));
        if (value === undefined || typeof label !== "string") {
          throw new Error(`[generate-settings] ${key} 包含未知选项`);
        }
        return { value, label };
      });
    }
    return {
      key,
      category,
      section,
      name: key.slice(category.length + 1),
      label,
      description: scalar(field.get("description")) ?? "",
      parentKey: scalar(field.get("parentKey")),
      ctrlType,
      options,
      hidden,
      local: storage === "local",
    };
  });
}

function readLocalDefaults(text, fields) {
  const name = "WORKBENCH_LOCAL_DEFAULTS";
  const entries = readProperties(readDeclaration(text, name), name);
  const known = new Set(fields.filter((field) => field.local).map((field) => field.key));
  for (const key of entries.keys()) {
    if (!known.has(key)) throw new Error(`[generate-settings] ${key} 是未知本机默认值`);
  }
  const defaults = new Map();
  for (const field of fields) {
    if (!field.local) continue;
    const value = scalar(entries.get(field.key));
    if (value === undefined) throw new Error(`[generate-settings] ${field.key} 缺少本机默认值`);
    if (
      !(field.ctrlType === "toggle" && typeof value === "boolean") &&
      !(field.ctrlType === "select" && field.options.some((option) => option.value === value))
    ) {
      throw new Error(`[generate-settings] ${field.key} 的本机默认值不匹配控件`);
    }
    defaults.set(field.key, value);
  }
  return defaults;
}

function readPresentationMap(text, name) {
  const map = readProperties(readDeclaration(text, name), name);
  return [...map].map(([key, node]) => {
    const entry = readProperties(node, `${name}.${key}`);
    const label = scalar(entry.get("label"));
    if (typeof label !== "string") throw new Error(`[generate-settings] ${name}.${key} 缺少标签`);
    return { key, label, group: scalar(entry.get("group")) };
  });
}

export function renderSettings(fieldsText, authText, localText) {
  const groups = readPresentationMap(fieldsText, "WORKBENCH_SETTING_GROUPS");
  const sections = readPresentationMap(fieldsText, "WORKBENCH_SETTING_SECTIONS");
  const runtimeFields = readFields(fieldsText);
  const localDefaults = readLocalDefaults(localText, runtimeFields);
  const fields = runtimeFields.filter((field) => !field.hidden);
  for (const field of fields) {
    const section = sections.find((entry) => entry.key === field.section);
    if (!section || !groups.some((group) => group.key === section.group)) {
      throw new Error(`[generate-settings] ${field.key} 缺少有效用途分组`);
    }
  }

  const defaultName = "DEFAULT_WORKBENCH_PREFERENCES";
  const preferences = readProperties(readDeclaration(authText, defaultName), defaultName);
  const categories = new Map(
    ["common", "image", "video", "pointcloud"].map((name) => [
      name,
      readProperties(preferences.get(name), `${defaultName}.${name}`),
    ]),
  );

  function renderDefault(field) {
    const node = categories.get(field.category)?.get(field.name);
    const value = field.local ? localDefaults.get(field.key) : scalar(node);
    if (!field.local && field.ctrlType === "labelContentByType" && node) return "—";
    if (value === undefined) throw new Error(`[generate-settings] ${field.key} 缺少可读取的默认值`);
    if (field.ctrlType === "toggle") return value ? "开启" : "关闭";
    if (field.ctrlType === "select") {
      const option = field.options.find((entry) => entry.value === value);
      return option ? option.label : String(value);
    }
    if (field.ctrlType === "text") return value === "" ? "空" : `\`${value}\``;
    return String(value);
  }

  const lines = [];
  lines.push("<!-- AUTO-GENERATED — 由 docs-site/scripts/generate-settings.mjs 从 -->");
  lines.push(
    "<!-- apps/web/src/pages/Workbench/state/workbenchSettingsFields.ts、workbenchLocalDefaults.ts 和 -->",
  );
  lines.push("<!-- apps/web/src/api/auth.ts 生成。请勿手改。 -->");
  lines.push("");

  const esc = (s) => s.replace(/\|/g, "\\|");

  for (const group of groups) {
    const groupSections = sections.filter(
      (entry) => entry.group === group.key && fields.some((field) => field.section === entry.key),
    );
    if (groupSections.length === 0) continue;
    lines.push(`### ${group.label}`, "");
    for (const section of groupSections) {
      const list = fields.filter((field) => field.section === section.key);
      lines.push(`#### ${section.label}`, "", "| 设置项 | 说明 | 默认 |", "|---|---|---|");
      for (const f of list) {
        const label = f.parentKey ? `└ ${f.label}` : f.label;
        lines.push(`| ${esc(label)} | ${esc(f.description)} | ${esc(renderDefault(f))} |`);
      }
      lines.push("");
    }
  }
  return { content: lines.join("\n"), count: fields.length };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { content, count } = renderSettings(
    readFileSync(fieldsSrc, "utf8"),
    readFileSync(authSrc, "utf8"),
    readFileSync(localSrc, "utf8"),
  );
  emitGenerated({ dst, content, label: "generate-settings", detail: `${count} fields` });
}
