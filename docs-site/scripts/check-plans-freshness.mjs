#!/usr/bin/env node

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const PLANS_DIR = resolve(here, "../../docs/plans");

const STALE_DAYS = 30;
const now = Date.now();
const cutoff = now - STALE_DAYS * 24 * 3600 * 1000;

const files = readdirSync(PLANS_DIR).filter((f) => f.endsWith(".md") && f !== "README.md");

let warned = 0;
for (const f of files) {
  const full = join(PLANS_DIR, f);
  const st = statSync(full);
  if (st.mtimeMs > cutoff) continue;

  const content = readFileSync(full, "utf8");
  if (/^>\s*Status:\s*abandoned/im.test(content)) continue;
  if (/^##\s+Outcome\b/im.test(content)) continue;

  const days = Math.floor((now - st.mtimeMs) / (24 * 3600 * 1000));
  const msg = `Plan ${f} is ${days} days old without ## Outcome — please summarize landed work or mark as abandoned`;
  console.log(`::warning file=docs/plans/${f}::${msg}`);
  warned++;
}

console.log(`Plans freshness check: ${warned} stale plan(s) without Outcome`);
