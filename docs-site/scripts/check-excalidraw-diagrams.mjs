#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const DEFAULT_DOCS_ROOT = path.resolve(path.dirname(SCRIPT_PATH), "..");
const DIAGRAM_PREFIX = "/diagrams/";
const EXCLUDED_DIRS = new Set([
  ".vitepress",
  "dev/examples",
  "maintainers",
  "node_modules",
  "public",
]);

function* walkFiles(root, predicate = () => true) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) yield* walkFiles(absolute, predicate);
    else if (entry.isFile() && predicate(entry.name)) yield absolute;
  }
}

function stripFencedCode(markdown) {
  let fence = null;
  return markdown
    .split("\n")
    .map((line) => {
      const match = line.match(/^\s*(`{3,}|~{3,})/);
      if (match) {
        const marker = match[1];
        if (!fence) fence = { char: marker[0], length: marker.length };
        else if (marker[0] === fence.char && marker.length >= fence.length) fence = null;
        return "";
      }
      if (fence) return "";
      return line.replace(/`[^`\n]*`/g, "");
    })
    .join("\n");
}

function isPublishedMarkdown(file, docsRoot) {
  const rel = path.relative(docsRoot, file).replace(/\\/g, "/");
  return ![...EXCLUDED_DIRS].some((dir) => rel === dir || rel.startsWith(`${dir}/`));
}

function normalizeReference(src, source, failures) {
  const clean = src.split(/[?#]/, 1)[0];
  if (!clean.startsWith(DIAGRAM_PREFIX)) {
    failures.push(`${source}: ExcalidrawDiagram src 必须位于 ${DIAGRAM_PREFIX}`);
    return null;
  }

  const rel = clean.slice(DIAGRAM_PREFIX.length);
  const normalized = path.posix.normalize(rel);
  if (!rel || normalized !== rel || rel.startsWith("../") || path.posix.isAbsolute(rel)) {
    failures.push(`${source}: 非法的图表路径 ${src}`);
    return null;
  }
  if (!rel.endsWith(".svg")) {
    failures.push(`${source}: ExcalidrawDiagram 只能引用 .svg 文件（${src}）`);
    return null;
  }
  return rel;
}

function collectReferences(docsRoot, failures) {
  const references = new Map();
  for (const file of walkFiles(docsRoot, (name) => name.endsWith(".md"))) {
    if (!isPublishedMarkdown(file, docsRoot)) continue;
    const source = path.relative(docsRoot, file).replace(/\\/g, "/");
    const markdown = stripFencedCode(fs.readFileSync(file, "utf8"));

    const componentRe = /<ExcalidrawDiagram\b([^>]*)>/g;
    let match;
    while ((match = componentRe.exec(markdown)) !== null) {
      const attributes = match[1];
      const src = attributes.match(/(?:^|\s)src\s*=\s*["']([^"']+)["']/)?.[1];
      const alt = attributes.match(/(?:^|\s)alt\s*=\s*["']([^"']+)["']/)?.[1];
      if (!src) {
        failures.push(`${source}: ExcalidrawDiagram 需要静态 src`);
        continue;
      }
      if (!alt?.trim()) failures.push(`${source}: ${src} 缺少非空 alt`);

      const rel = normalizeReference(src, source, failures);
      if (!rel) continue;
      const sources = references.get(rel) ?? new Set();
      sources.add(source);
      references.set(rel, sources);
    }

    const rawImagePatterns = [
      /!\[[^\]]*\]\((?:<)?(\/diagrams\/[^\s)>]+)(?:>)?(?:\s+["'][^"']*["'])?\)/g,
      /<img\b[^>]*\bsrc\s*=\s*["'](\/diagrams\/[^"']+)["'][^>]*>/g,
    ];
    for (const pattern of rawImagePatterns) {
      while ((match = pattern.exec(markdown)) !== null) {
        failures.push(`${source}: ${match[1]} 应使用 <ExcalidrawDiagram> 引用`);
      }
    }
  }
  return references;
}

function validateSvg(file, rel, failures) {
  const svg = fs.readFileSync(file, "utf8");
  const openingTag = svg.match(/<svg\b[^>]*>/i)?.[0];
  if (!openingTag) {
    failures.push(`${rel}: 不是有效的 SVG`);
    return;
  }

  const viewBox = openingTag.match(/\bviewBox\s*=\s*["']\s*([^"']+?)\s*["']/i)?.[1];
  const values =
    viewBox
      ?.trim()
      .split(/[\s,]+/)
      .map(Number) ?? [];
  if (
    values.length !== 4 ||
    values.some((value) => !Number.isFinite(value)) ||
    values[2] <= 0 ||
    values[3] <= 0
  ) {
    failures.push(`${rel}: SVG 需要宽高为正数的 viewBox`);
  }
  if (!/<!--\s*svg-source:excalidraw\s*-->/i.test(svg)) {
    failures.push(`${rel}: 缺少 svg-source:excalidraw 标记`);
  }
  if (!/payload-type:application\/vnd\.excalidraw\+json/i.test(svg)) {
    failures.push(`${rel}: 未内嵌 Excalidraw scene payload`);
  }

  const payload = svg.match(/<!--\s*payload-start\s*-->([\s\S]*?)<!--\s*payload-end\s*-->/i)?.[1];
  if (!payload || payload.trim().length < 16) {
    failures.push(`${rel}: Excalidraw scene payload 为空或不完整`);
  } else {
    try {
      const envelope = JSON.parse(Buffer.from(payload.trim(), "base64").toString("latin1"));
      if (envelope.encoding !== "bstring" || typeof envelope.encoded !== "string") {
        throw new Error("未知的 payload 编码");
      }
      const encoded = Buffer.from(envelope.encoded, "latin1");
      const sceneJson = envelope.compressed
        ? inflateSync(encoded).toString("utf8")
        : encoded.toString("utf8");
      const scene = JSON.parse(sceneJson);
      if (
        scene.type !== "excalidraw" ||
        !Array.isArray(scene.elements) ||
        !scene.appState ||
        typeof scene.appState !== "object"
      ) {
        throw new Error("缺少 Excalidraw scene 字段");
      }
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      failures.push(`${rel}: Excalidraw scene payload 无法解码（${reason}）`);
    }
  }
  if (!/\bVirgil\b/.test(svg) || !/data:font\/woff2?;base64,/i.test(svg)) {
    failures.push(`${rel}: 需要以 data URI 内嵌 Virgil 字体`);
  }
  if (/<script\b/i.test(svg)) failures.push(`${rel}: SVG 不得包含 script`);
  if (/(?:href|src)\s*=\s*["'](?:https?:)?\/\//i.test(svg)) {
    failures.push(`${rel}: SVG 不得引用外部资源`);
  }
}

export function checkExcalidrawDiagrams(docsRoot = DEFAULT_DOCS_ROOT) {
  const diagramsRoot = path.join(docsRoot, "public/diagrams");
  const failures = [];
  if (!fs.existsSync(diagramsRoot)) {
    return {
      assets: [],
      references: new Map(),
      failures: [`图表目录不存在：${path.relative(docsRoot, diagramsRoot)}`],
    };
  }

  const references = collectReferences(docsRoot, failures);
  const assets = [];
  for (const file of walkFiles(diagramsRoot)) {
    const rel = path.relative(diagramsRoot, file).replace(/\\/g, "/");
    if (rel === ".gitkeep") continue;
    if (!rel.endsWith(".svg")) {
      failures.push(`${rel}: diagrams/ 只允许内嵌 scene 的 .svg 文件`);
      continue;
    }
    if (
      !rel
        .split("/")
        .every(
          (segment) =>
            /^[a-z0-9][a-z0-9-]*\.svg$/.test(segment) || /^[a-z0-9][a-z0-9-]*$/.test(segment),
        )
    ) {
      failures.push(`${rel}: 路径只能使用小写字母、数字和连字号`);
    }
    assets.push(rel);
    validateSvg(file, rel, failures);
  }

  const assetSet = new Set(assets);
  for (const [rel, sources] of references) {
    if (!assetSet.has(rel)) {
      failures.push(
        `${DIAGRAM_PREFIX}${rel}: 引用自 ${[...sources].sort().join(", ")}，但文件不存在`,
      );
    }
  }
  for (const rel of assets) {
    if (!references.has(rel)) failures.push(`${rel}: 图表未被任何已发布文档引用`);
  }

  return {
    assets: assets.sort(),
    references,
    failures: [...new Set(failures)].sort(),
  };
}

function report(result) {
  console.log(
    `\nExcalidraw 图表校验 — ${result.assets.length} 张图 · ${result.references.size} 个引用\n${"─".repeat(64)}`,
  );
  for (const failure of result.failures) console.log(`✗  ${failure}`);
  if (result.failures.length === 0) {
    console.log("✓  引用、内嵌 scene 与手写字体合同一致");
  }
  console.log("");
}

if (process.argv[1] && path.resolve(process.argv[1]) === SCRIPT_PATH) {
  const rootIndex = process.argv.indexOf("--root");
  const docsRoot = rootIndex >= 0 ? path.resolve(process.argv[rootIndex + 1]) : DEFAULT_DOCS_ROOT;
  const result = checkExcalidrawDiagrams(docsRoot);
  report(result);
  if (result.failures.length > 0) process.exit(1);
}
