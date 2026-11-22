import { version as appVersion } from "../../package.json";

export { appVersion };

export interface ReleaseNoteGroup {
  key: string;
  label: string;
  items: string[];
}

export interface ReleaseNotes {
  version: string;
  date: string | null;
  groups: ReleaseNoteGroup[];
}

const GROUP_LABELS: Record<string, string> = {
  Added: "新增",
  Changed: "变更",
  Deprecated: "弃用",
  Removed: "移除",
  Fixed: "修复",
  Security: "安全",
};

const RELEASE_HEADING = /^## \[([^\]]+)\](?:\s+-\s+(.+))?$/;
const GROUP_HEADING = /^###\s+(.+)$/;

export function parseChangelogSection(markdown: string, version: string): ReleaseNotes | null {
  const lines = markdown.split(/\r?\n/);
  let start = -1;
  let date: string | null = null;
  for (let i = 0; i < lines.length; i++) {
    const match = RELEASE_HEADING.exec(lines[i].trim());
    if (!match) continue;
    if (match[1] === version) {
      start = i;
      date = match[2]?.trim() ?? null;
      break;
    }
  }
  if (start === -1) return null;

  const groups: ReleaseNoteGroup[] = [];
  let current: ReleaseNoteGroup | null = null;
  let continuing = false;
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (RELEASE_HEADING.test(line)) break;
    if (line === "") {
      continuing = false;
      continue;
    }
    const groupMatch = GROUP_HEADING.exec(line);
    if (groupMatch) {
      const key = groupMatch[1].trim();
      current = { key, label: GROUP_LABELS[key] ?? key, items: [] };
      groups.push(current);
      continuing = false;
      continue;
    }
    if (line.startsWith("- ")) {
      if (current) current.items.push(line.slice(2).trim());
      continuing = true;
      continue;
    }
    if (continuing && current && current.items.length > 0) {
      const last = current.items.length - 1;
      current.items[last] = `${current.items[last]} ${line}`;
      continue;
    }
    continuing = false;
  }
  return { version, date, groups };
}

export function compareSemver(a: string, b: string): number {
  const pa = semverTriple(a);
  const pb = semverTriple(b);
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
  }
  return 0;
}

function semverTriple(value: string): [number, number, number] {
  const core = value.trim().replace(/^v/i, "").split("+")[0].split("-")[0];
  const parts = core.split(".");
  const at = (i: number) => {
    const parsed = Number.parseInt(parts[i] ?? "", 10);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  };
  return [at(0), at(1), at(2)];
}

export function shouldShowReleaseNotes(seen: string | null | undefined, current: string): boolean {
  if (!current) return false;
  return compareSemver(current, seen ?? "") > 0;
}

let cachedNotes: ReleaseNotes | null | undefined;

export async function loadCurrentReleaseNotes(): Promise<ReleaseNotes | null> {
  if (cachedNotes === undefined) {
    const { sectionMarkdown } = await import("virtual:release-notes");
    cachedNotes = parseChangelogSection(sectionMarkdown, appVersion);
  }
  return cachedNotes;
}
