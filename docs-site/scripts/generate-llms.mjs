import { readFileSync, writeFileSync, readdirSync, statSync, mkdirSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__here, "..");
const PUBLIC = join(ROOT, "public");

const SITE = "https://yoao-labs.github.io/ai-annotation-platform";
const TITLE = "YOAO";
const DESC =
  "一站式 AI 辅助标注平台：图像 / 视频 / 点云标注、AI 预标注、质量审核、私有化部署。文档覆盖使用平台、开发者、API 与部署运维四个域。";

const SECTIONS = [
  { dir: "user-guide", label: "使用平台" },
  { dir: "dev", label: "开发者" },
  { dir: "api", label: "API" },
  { dir: "ops", label: "部署运维" },
];

const SKIP = /(^|\/)(adr|changelog|roadmap)\/|_routes\.generated|\/examples\//;

function walk(dir) {
  const abs = join(ROOT, dir);
  const out = [];
  const rec = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      const st = statSync(p);
      if (st.isDirectory()) rec(p);
      else if (name.endsWith(".md")) out.push(p);
    }
  };
  rec(abs);
  return out.filter((f) => !SKIP.test(relative(ROOT, f).replace(/\\/g, "/")));
}

function stripFrontmatter(raw) {
  return raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "");
}

function firstH1(body) {
  return body.match(/^#\s+(.+?)\s*$/m)?.[1]?.trim() ?? null;
}

function firstParagraph(body) {
  const lines = body.split(/\r?\n/);
  let inFence = false;
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (inFence || !t) continue;
    if (/^(#|>|<|!\[|\|)/.test(t)) continue;
    if (/^[-*] /.test(t)) continue;
    return t
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/[*_`]/g, "")
      .replace(/\s+/g, " ")
      .slice(0, 120);
  }
  return null;
}

function urlFor(file) {
  let rel = relative(ROOT, file).replace(/\\/g, "/").replace(/\.md$/, "");
  rel = rel.replace(/\/index$/, "").replace(/^index$/, "");
  return rel ? `${SITE}/${rel}` : `${SITE}/`;
}

function sortPages(files) {
  return files.sort((a, b) => {
    const ai = /\/index\.md$|(^|\/)[^/]+\/index\.md$/.test(a) || a.endsWith("/index.md");
    const bi = b.endsWith("/index.md");
    if (ai && !bi) return -1;
    if (!ai && bi) return 1;
    return a.localeCompare(b);
  });
}

function cleanBody(body) {
  return body
    .split(/\r?\n/)
    .filter((l) => !/^\s*<\/?(div|script|style|ApiReferenceFrame|DocLinkCard|DocsHome)\b/.test(l))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

const bySection = SECTIONS.map((s) => ({ ...s, pages: sortPages(walk(s.dir)) }));

let llms = `# ${TITLE}\n\n> ${DESC}\n\n`;
llms += `完整正文语料：[llms-full.txt](${SITE}/llms-full.txt) ｜ 机器可读 API 契约：[openapi.json](${SITE}/openapi.json)\n`;
for (const s of bySection) {
  llms += `\n## ${s.label}\n\n`;
  for (const f of s.pages) {
    const body = stripFrontmatter(readFileSync(f, "utf8"));
    const title = firstH1(body) ?? relative(ROOT, f);
    const desc = firstParagraph(body);
    llms += `- [${title}](${urlFor(f)})${desc ? `: ${desc}` : ""}\n`;
  }
}
mkdirSync(PUBLIC, { recursive: true });
writeFileSync(join(PUBLIC, "llms.txt"), llms, "utf8");

let full = `# ${TITLE} — 全文文档\n\n> ${DESC}\n\n来源：${SITE}\n`;
let pageCount = 0;
for (const s of bySection) {
  for (const f of s.pages) {
    const body = stripFrontmatter(readFileSync(f, "utf8"));
    const title = firstH1(body) ?? relative(ROOT, f);
    full += `\n\n---\n\n# ${title}\n\n来源：${urlFor(f)}\n\n${cleanBody(body)}\n`;
    pageCount++;
  }
}
writeFileSync(join(PUBLIC, "llms-full.txt"), full, "utf8");

const kb = (s) => Math.round(Buffer.byteLength(s, "utf8") / 1024);
console.log(
  `[generate-llms] llms.txt (${kb(llms)}KB) + llms-full.txt (${kb(full)}KB, ${pageCount} 页) 已生成`,
);
