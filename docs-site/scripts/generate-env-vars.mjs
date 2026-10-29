#!/usr/bin/env node
/**
 * 从仓库根目录的 .env.example 自动生成 docs-site/dev/reference/env-vars.md。
 *
 * 解析规则：
 *   - 以 `# ===` 开头的行视为分组标题（提取括号内文字作为 H2 标题）
 *   - 以 `# ` 开头的普通注释行累积为下一个变量的说明
 *   - `KEY=VALUE` 行生成表格行（注释掉的 `# KEY=VALUE` 也包含，标注为可选）
 *
 * 用法：node docs-site/scripts/generate-env-vars.mjs
 *   在 package.json 中加入 "docs:gen-env-vars": "node docs-site/scripts/generate-env-vars.mjs"
 */

import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { currentDocumentDate } from "./document-date.mjs";

const __here = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__here, "../..");
const ENV_EXAMPLE = join(REPO_ROOT, ".env.example");
const OUTPUT = join(__here, "../dev/reference/env-vars.md");

const today = currentDocumentDate();

const HEADER = `---
title: 环境变量参考
audience: [dev, ops]
type: reference
since: v0.9.0
status: stable
last_reviewed: ${today}
---

# 环境变量参考

> **自动生成说明**：本页由 \`docs-site/scripts/generate-env-vars.mjs\` 从 \`.env.example\` 生成。
> 修改环境变量说明请编辑 \`.env.example\` 中的注释，再运行 \`pnpm docs:gen-env-vars\`。

`;

function stripReleaseMarkers(text) {
  return (
    text
      .replace(/\bv\d+\.\d+(?:\.\d+)?\+?\s*[·:：]\s*/g, "")
      .replace(/§[A-Z]\s*[·:：]\s*/g, "")
      .replace(/\s*[（(]v\d+\.\d+(?:\.\d+)?\+?(?:[^）)]*)[）)]/g, "")
      .replace(/\bv\d+\.\d+(?:\.\d+)?\+?\s*/g, "")
      .replace(/,\s*[）)]/g, ")")
      // 仅清掉「带空白」的空括号（例如版本标记被剥光后留下的 `(  )`），
      // 保留无空白的空括号 `()`，避免误伤代码片段里的 `decode()` / `generate_key()` 等。
      .replace(/[（(]\s+[）)]/g, "")
  );
}

function parseEnvExample(content) {
  const lines = content.split("\n");
  const sections = [];
  let currentSection = null;
  let pendingComments = [];

  for (const line of lines) {
    const trimmed = line.trim();

    // Section header: # ===... or # ---...
    if (/^#\s*={4,}/.test(trimmed) || /^#\s*-{4,}/.test(trimmed)) {
      continue;
    }

    // Section title comment: # 数据库 (PostgreSQL)
    if (/^#\s+\S/.test(trimmed) && currentSection === null) {
      const title = stripReleaseMarkers(trimmed.replace(/^#\s+/, ""));
      currentSection = { title, rows: [] };
      sections.push(currentSection);
      pendingComments = [];
      continue;
    }

    if (!currentSection) {
      currentSection = { title: "其他", rows: [] };
      sections.push(currentSection);
    }

    // New section marker
    if (/^#\s*={4,}/.test(trimmed)) {
      currentSection = null;
      pendingComments = [];
      continue;
    }

    // Variable comment (accumulate for next var)
    if (/^#\s+[^=]/.test(trimmed) && !/^#\s*(=+|-+)/.test(trimmed)) {
      // Check if it looks like a commented-out var definition
      const commentedVar = trimmed.match(/^#\s*([\w_]+)=(.*)$/);
      if (commentedVar) {
        const [, key, defaultVal] = commentedVar;
        const desc = stripReleaseMarkers(pendingComments.join(" ").trim());
        pendingComments = [];
        currentSection.rows.push({
          key,
          defaultVal: defaultVal.trim() || "—",
          desc,
          optional: true,
        });
      } else {
        pendingComments.push(stripReleaseMarkers(trimmed.replace(/^#\s*/, "")));
      }
      continue;
    }

    // Empty comment line — reset pending
    if (trimmed === "#") {
      pendingComments = [];
      continue;
    }

    // Variable definition
    const varMatch = trimmed.match(/^([\w_]+)=(.*)$/);
    if (varMatch) {
      const [, key, defaultVal] = varMatch;
      const desc = stripReleaseMarkers(pendingComments.join(" ").trim());
      pendingComments = [];
      currentSection.rows.push({
        key,
        defaultVal: defaultVal.trim() || "—",
        desc,
        optional: false,
      });
      continue;
    }

    // Empty line — end of group, next non-# starts new section
    if (trimmed === "") {
      pendingComments = [];
      if (currentSection && currentSection.rows.length > 0) {
        currentSection = null;
      }
    }
  }

  return sections.filter((s) => s.rows.length > 0);
}

function renderSections(sections) {
  return sections
    .map((section) => {
      const rows = section.rows
        .map((r) => {
          const description = (r.desc || "—").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
          return `| \`${r.key}\` | \`${r.defaultVal || "—"}\` | ${description} |`;
        })
        .join("\n");
      return `## ${section.title}\n\n| 变量 | 默认值 | 说明 |\n|---|---|---|\n${rows}\n`;
    })
    .join("\n");
}

const envContent = readFileSync(ENV_EXAMPLE, "utf8");
const sections = parseEnvExample(envContent);
const output = HEADER + renderSections(sections);

writeFileSync(OUTPUT, output);
console.log(
  `Generated: ${OUTPUT} (${sections.length} sections, ${sections.reduce((a, s) => a + s.rows.length, 0)} vars)`,
);
