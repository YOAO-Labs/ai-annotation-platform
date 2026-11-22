#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { rewrites } from "../.vitepress/content.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const docsRoot = path.resolve(here, "..");
const strict = process.argv.includes("--strict");

const SKIP_ROOTS = new Set([
  ".vitepress",
  "changelog",
  "dev/adr",
  "dev/examples",
  "maintainers",
  "node_modules",
  "public",
  "roadmap",
  "scripts",
]);
const EXEMPT_ROUTES = new Map([
  ["/user-guide/projects/annotation-guide", "legacy URL redirects to the current projects guide"],
]);

function relativeToDocs(file) {
  return path.relative(docsRoot, file).replace(/\\/g, "/");
}

function walk(dir, files = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      const child = path.join(dir, entry.name);
      if (!SKIP_ROOTS.has(relativeToDocs(child))) walk(child, files);
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      const file = path.join(dir, entry.name);
      const rel = relativeToDocs(file);
      if (!entry.name.endsWith(".generated.md")) files.push(file);
    }
  }
  return files;
}

function routeForFile(file) {
  const sourceRel = relativeToDocs(file);
  const rel = rewrites[sourceRel] ?? sourceRel;
  if (rel === "index.md") return "/";
  if (rel.endsWith("/index.md")) return `/${rel.slice(0, -"index.md".length)}`;
  return `/${rel.slice(0, -".md".length)}`;
}

function normalizeRoute(route) {
  const normalized = path.posix.normalize(route).replace(/^\.(?=\/)/, "");
  return normalized === "/." ? "/" : normalized.replace(/\/$/, "") || "/";
}

function routeForTarget(target, sourceFile) {
  const clean = target.split(/[?#]/, 1)[0];
  if (!clean || clean.startsWith("#") || /^(?:[a-z]+:|\/\/)/i.test(clean)) return null;

  if (clean.startsWith("/")) return normalizeRoute(clean);

  const resolved = path.resolve(path.dirname(sourceFile), clean);
  const rel = path.relative(docsRoot, resolved);
  if (rel.startsWith("..") || path.isAbsolute(rel)) return null;
  if (path.extname(rel) && path.extname(rel) !== ".md") return null;
  return normalizeRoute(routeForFile(resolved.endsWith(".md") ? resolved : `${resolved}.md`));
}

function recordLinks(content, sourceFile, discovered) {
  const inline = /\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
  let match;
  while ((match = inline.exec(content)) !== null) {
    const route = routeForTarget(match[1], sourceFile);
    if (route) discovered.add(route);
  }
}

const pages = walk(docsRoot);
const pageRoutes = new Map(pages.map((file) => [normalizeRoute(routeForFile(file)), file]));
const discovered = new Set(["/"]);

for (const page of pages) recordLinks(fs.readFileSync(page, "utf8"), page, discovered);

const navigationRoot = path.join(docsRoot, ".vitepress", "navigation");
const navigationSources = [path.join(docsRoot, ".vitepress", "config.ts")];
if (fs.existsSync(navigationRoot)) {
  for (const entry of fs.readdirSync(navigationRoot, { withFileTypes: true })) {
    if (entry.isFile() && entry.name.endsWith(".ts")) {
      navigationSources.push(path.join(navigationRoot, entry.name));
    }
  }
}

const configLink = /\blink:\s*["']([^"']+)["']/g;
for (const navigationSource of navigationSources) {
  const config = fs.readFileSync(navigationSource, "utf8");
  configLink.lastIndex = 0;
  let match;
  while ((match = configLink.exec(config)) !== null) {
    const route = routeForTarget(match[1], path.join(docsRoot, "index.md"));
    if (route) discovered.add(route);
  }
}

const missing = [...pageRoutes.keys()]
  .filter((route) => !discovered.has(route) && !EXEMPT_ROUTES.has(route))
  .sort();

console.log(`Navigation coverage — ${pageRoutes.size} rendered pages`);
if (missing.length === 0) {
  console.log("✓ All pages are reachable from navigation or another rendered page.");
} else {
  for (const route of missing) console.log(`✗ ${route}`);
  console.log(
    `\n${missing.length} page(s) are not reachable. Add a navigation or page link, or record an intentional exemption.`,
  );
}

if (EXEMPT_ROUTES.size > 0) {
  console.log("\nIntentional exemptions:");
  for (const [route, reason] of EXEMPT_ROUTES) console.log(`○ ${route} — ${reason}`);
}

if (strict && missing.length > 0) process.exit(1);
