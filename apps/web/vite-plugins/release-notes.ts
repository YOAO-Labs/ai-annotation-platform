import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Plugin } from "vite";

const VIRTUAL_ID = "virtual:release-notes";
const RESOLVED_ID = "\0" + VIRTUAL_ID;

export function releaseNotesPlugin(): Plugin {
  return {
    name: "vite-plugin-release-notes",
    resolveId(id) {
      if (id === VIRTUAL_ID) return RESOLVED_ID;
      return null;
    },
    load(id) {
      if (id !== RESOLVED_ID) return null;
      const webRoot = resolve(__dirname, "..");
      const version = JSON.parse(readFileSync(resolve(webRoot, "package.json"), "utf8"))
        .version as string;
      let markdown = "";
      try {
        markdown = readFileSync(resolve(webRoot, "../../CHANGELOG.md"), "utf8");
      } catch {
        markdown = "";
      }
      return `export const sectionMarkdown = ${JSON.stringify(
        extractChangelogSection(markdown, version),
      )};`;
    },
  };
}

function extractChangelogSection(markdown: string, version: string): string {
  const escaped = version.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const start = markdown.search(new RegExp(`^## \\[${escaped}\\]`, "m"));
  if (start === -1) return "";
  const rest = markdown.slice(start + 1);
  const next = rest.search(/^## /m);
  return next === -1 ? markdown.slice(start) : markdown.slice(start, start + 1 + next);
}
