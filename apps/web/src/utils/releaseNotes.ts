/**
 * 更新内容来自仓库根 `CHANGELOG.md` 当前版本段落,由 `vite-plugins/release-notes.ts`
 * 构建期裁剪注入:发布流程(aap-release)同步 bump 三处版本号并写 changelog,构建产物
 * 里的段落与 package.json 版本天然一致;dev 模式修改 CHANGELOG 后重启 dev server 生效。
 */
import { version as appVersion } from "../../package.json";

export { appVersion };

export interface ReleaseNoteGroup {
  /** Keep a Changelog 分组键,如 "Added"。 */
  key: string;
  /** 中文分组标签,如「新增」;未知分组键原样展示。 */
  label: string;
  /** 该分组的条目(软换行已并入同一条)。 */
  items: string[];
}

export interface ReleaseNotes {
  version: string;
  /** 标题行 `## [x.y.z] - YYYY-MM-DD` 中的日期;缺省 null。 */
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

/**
 * 提取指定版本的 changelog 段落(`## [version] - date` 到下一个 `## ` 之间),
 * 按 `### 分组` 组织条目;找不到该版本标题时返回 null。
 */
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
  // 上一非空行是条目内容(bullet 或接续行)时为 true,用于识别 bullet 的软换行。
  let continuing = false;
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (RELEASE_HEADING.test(line)) break; // 下一个版本段落,结束。
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
    // 非 bullet 的接续行:仅紧跟上一条内容时并入(软换行);独立段落忽略。
    if (continuing && current && current.items.length > 0) {
      const last = current.items.length - 1;
      current.items[last] = `${current.items[last]} ${line}`;
      continue;
    }
    continuing = false;
  }
  return { version, date, groups };
}

/** 语义化版本主段落比较(x.y.z);解析不了的段按 0 处理,因此空串小于任何正式版本。 */
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

/**
 * 是否需要弹「本次更新」:仅在构建版本比账号已确认版本新时为真。
 * 相等(已确认过)不弹;已确认版本更高(部署回滚)也不弹,避免回滚期间反复打扰。
 */
export function shouldShowReleaseNotes(seen: string | null | undefined, current: string): boolean {
  if (!current) return false;
  return compareSemver(current, seen ?? "") > 0;
}

let cachedNotes: ReleaseNotes | null | undefined;

/**
 * 当前构建版本的更新要点。按需从 `virtual:release-notes` 独立 chunk 加载(段落文本
 * 数十 KB,不进主包预算;登录弹窗时才拉取,gzip 后约 10KB)。版本已 bump 但
 * changelog 尚未写入时为 null,弹窗回落为简短提示(发布流程要求两者同步,正常不会
 * 出现)。结果进程内缓存,同会话多次调用不重复拉取。
 */
export async function loadCurrentReleaseNotes(): Promise<ReleaseNotes | null> {
  if (cachedNotes === undefined) {
    const { sectionMarkdown } = await import("virtual:release-notes");
    cachedNotes = parseChangelogSection(sectionMarkdown, appVersion);
  }
  return cachedNotes;
}
