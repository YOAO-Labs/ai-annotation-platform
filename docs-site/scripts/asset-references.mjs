import fs from "node:fs";
import path from "node:path";
import {
  extractImageReferences,
  IMAGE_EXTENSION_RE,
  resolveLocalMediaPath,
  walkFiles,
  withoutMarkdownCode,
} from "../../scripts/image-reference-utils.mjs";

function repoKey(absolute, repoRoot) {
  return path.relative(repoRoot, absolute).replaceAll("\\", "/");
}

function addRecord(records, absolute, source, repoRoot) {
  const key = repoKey(absolute, repoRoot);
  if (key === ".." || key.startsWith("../") || path.isAbsolute(key)) return;
  let record = records.get(key);
  if (!record) {
    record = { absolute, sources: new Set() };
    records.set(key, record);
  }
  record.sources.add(source);
}

export function collectAssetReferences(repoRoot) {
  const docsRoot = path.join(repoRoot, "docs-site");
  const records = new Map();
  for (const markdownPath of walkFiles(docsRoot, (name) => name.endsWith(".md"))) {
    const content = withoutMarkdownCode(fs.readFileSync(markdownPath, "utf8"));
    const source = repoKey(markdownPath, repoRoot);
    for (const src of extractImageReferences(content)) {
      const absolute = resolveLocalMediaPath(src, markdownPath, docsRoot);
      if (absolute) addRecord(records, absolute, source, repoRoot);
    }
    for (const tag of content.matchAll(/<(?:DocsVideo|video|source)\b[^>]*>/g)) {
      for (const [, src] of tag[0].matchAll(/\s(?:src|poster)\s*=\s*["']([^"']+)["']/g)) {
        const absolute = resolveLocalMediaPath(src, markdownPath, docsRoot);
        if (absolute) addRecord(records, absolute, source, repoRoot);
      }
    }
  }

  const themeRoot = path.join(docsRoot, ".vitepress/theme");
  for (const sourcePath of walkFiles(themeRoot, (name) => /\.(?:vue|ts)$/.test(name))) {
    const content = fs.readFileSync(sourcePath, "utf8");
    const source = repoKey(sourcePath, repoRoot);
    for (const match of content.matchAll(/\bimport\s+[\w$]+\s+from\s*["'](\.{1,2}\/[^"']+)["']/g)) {
      const absolute = resolveLocalMediaPath(match[1], sourcePath, docsRoot);
      if (absolute && IMAGE_EXTENSION_RE.test(absolute))
        addRecord(records, absolute, source, repoRoot);
    }
    for (const [, poster, src] of content.matchAll(
      /(\bposter\s*:\s*)?\bwithBase\(\s*["'](\/[^"']+)["']\s*\)/g,
    )) {
      const absolute = resolveLocalMediaPath(src, sourcePath, docsRoot);
      if (!absolute) continue;
      if (poster || /\.(?:mp4|webm)$/i.test(absolute) || IMAGE_EXTENSION_RE.test(absolute))
        addRecord(records, absolute, source, repoRoot);
    }
  }
  return records;
}
