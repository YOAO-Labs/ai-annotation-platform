import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expectedMatrixTargets, SCREENSHOT_MATRIX_PROJECTS } from "./matrix";
import { SCENES } from "./scenes";

interface ScreenshotRunMetadata {
  target: string;
  scene: string;
  source: string;
  capture: unknown;
  fixture: unknown;
  seed_revision: string;
  project: string;
  viewport: { width: number; height: number } | null;
  theme: string;
  clock?: "fixed" | "live";
  locale: string;
  browser: { name: string; version: string };
}

type LegacyEntry = Record<string, unknown> & {
  auto?: boolean;
  lastRun?: string;
  generated_at?: string;
  note?: string;
};

const HERE = decodeURIComponent(new URL(".", import.meta.url).pathname);
const REPO_ROOT = HERE.replace(/\/apps\/web\/e2e\/screenshots\/?$/, "");
const MANIFEST_PATH = path.join(REPO_ROOT, "apps/web/e2e/screenshots/outputs/manifest.json");
function currentCommit(): string {
  if (process.env.GITHUB_SHA) return process.env.GITHUB_SHA;
  return execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).trim();
}

function currentWorktreeDirty(): boolean {
  return (
    execFileSync("git", ["status", "--porcelain", "--untracked-files=normal"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    }).trim().length > 0
  );
}

function fileCommit(target: string): string {
  try {
    return (
      execFileSync("git", ["log", "-1", "--format=%H", "--", target], {
        cwd: REPO_ROOT,
        encoding: "utf8",
      }).trim() || currentCommit()
    );
  } catch {
    return currentCommit();
  }
}

function fileFacts(target: string) {
  const absolute = path.join(REPO_ROOT, target);
  const buffer = fs.readFileSync(absolute);
  if (
    buffer.length < 24 ||
    !buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
  ) {
    throw new Error(`screenshot manifest 只接受 PNG 静态资产: ${target}`);
  }
  return {
    sha256: createHash("sha256").update(buffer).digest("hex"),
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
  };
}

function readExistingEntries(): Record<string, LegacyEntry> {
  if (!fs.existsSync(MANIFEST_PATH)) return {};
  const raw = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8")) as Record<string, unknown> & {
    schema_version?: number;
    entries?: Record<string, LegacyEntry>;
  };
  if (raw.schema_version === 2 && raw.entries) {
    for (const target in raw.entries) {
      const entry = raw.entries[target];
      entry.source_commit ??= raw.source_commit;
      entry.source_worktree_dirty ??= raw.source_worktree_dirty;
      entry.seed_revision ??= raw.seed_revision;
    }
    return raw.entries;
  }
  return raw as Record<string, LegacyEntry>;
}

function sortedRecord<T>(entries: Array<[string, T]>): Record<string, T> {
  return Object.fromEntries(entries.sort(([left], [right]) => left.localeCompare(right)));
}

export default class ScreenshotManifestReporter implements Reporter {
  private runEntries = new Map<string, ScreenshotRunMetadata>();
  private reporterError: string | null = null;
  private sourceWorktreeDirty = currentWorktreeDirty();

  onTestEnd(_test: TestCase, result: TestResult): void {
    if (result.status !== "passed") return;
    for (const attachment of result.attachments) {
      if (attachment.name !== "screenshot-manifest" || !attachment.body) continue;
      const metadata = JSON.parse(attachment.body.toString("utf8")) as ScreenshotRunMetadata;
      if (this.runEntries.has(metadata.target)) {
        this.reporterError = `重复 screenshot manifest target: ${metadata.target}`;
        return;
      }
      this.runEntries.set(metadata.target, metadata);
    }
  }

  async onEnd(result: FullResult): Promise<{ status?: FullResult["status"] } | void> {
    try {
      return this.rebuildManifest(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[manifest] ✗ ${message}`);
      return { status: "failed" };
    }
  }

  private rebuildManifest(result: FullResult): void {
    if (this.reporterError) throw new Error(this.reporterError);
    const migrateLegacy = process.env.SCREENSHOT_MIGRATE_MANIFEST === "1";
    if (this.runEntries.size === 0) return;
    const expectedTargets = expectedMatrixTargets(SCENES);
    const missingTargets = [...expectedTargets].filter((target) => !this.runEntries.has(target));
    const runProjects = [
      ...new Set([...this.runEntries.values()].map((entry) => entry.project)),
    ].sort();
    const isCompleteMatrix =
      missingTargets.length === 0 &&
      SCREENSHOT_MATRIX_PROJECTS.every((project) => runProjects.includes(project));
    if (!isCompleteMatrix && migrateLegacy) {
      throw new Error(`manifest 迁移要求完整矩阵，缺少 ${missingTargets.length} 个 target`);
    }
    if (!migrateLegacy && process.env.SCREENSHOT_VALIDATE_ONLY === "1") return;
    if (result.status !== "passed") {
      throw new Error("screenshot 矩阵未全部通过，拒绝替换 manifest");
    }

    const existing = readExistingEntries();
    const generatedAt = new Date().toISOString();
    const sourceCommit = currentCommit();
    const outputEntries: Array<[string, Record<string, unknown>]> = [];

    for (const [target, entry] of Object.entries(existing)) {
      if (!isCompleteMatrix && !this.runEntries.has(target)) {
        outputEntries.push([target, entry]);
        continue;
      }
      if (entry.auto !== false) continue;
      const absolute = path.join(REPO_ROOT, target);
      if (!fs.existsSync(absolute)) {
        if (migrateLegacy) {
          console.warn(`[manifest] 跳过不存在的旧手动资产: ${target}`);
          continue;
        }
        throw new Error(`手动资产已登记但文件不存在: ${target}`);
      }
      outputEntries.push([
        target,
        {
          auto: false,
          target,
          note: entry.note,
          generated_at: entry.generated_at ?? entry.lastRun ?? generatedAt,
          provenance: "manual",
          ...fileFacts(target),
        },
      ]);
    }

    for (const [target, metadata] of this.runEntries) {
      const absolute = path.join(REPO_ROOT, target);
      if (!fs.existsSync(absolute)) {
        if (migrateLegacy) continue;
        throw new Error(`截图测试通过但目标文件不存在: ${target}`);
      }
      const previous = existing[target];
      outputEntries.push([
        target,
        {
          auto: true,
          target,
          scene: metadata.scene,
          source: metadata.source,
          capture: metadata.capture,
          fixture: migrateLegacy ? null : metadata.fixture,
          seed_profile: "screenshots",
          seed_revision: migrateLegacy ? "legacy-pre-catalog" : metadata.seed_revision,
          source_commit: migrateLegacy ? fileCommit(target) : sourceCommit,
          source_worktree_dirty: migrateLegacy ? null : this.sourceWorktreeDirty,
          browser: migrateLegacy ? { name: "chromium", version: "unknown" } : metadata.browser,
          project: metadata.project,
          viewport: metadata.viewport,
          theme: metadata.theme,
          clock: migrateLegacy ? "unverified" : (metadata.clock ?? "fixed"),
          locale: metadata.locale,
          generated_at: migrateLegacy
            ? (previous?.generated_at ??
              previous?.lastRun ??
              fs.statSync(absolute).mtime.toISOString())
            : generatedAt,
          provenance: migrateLegacy ? "legacy" : "current-run",
          ...fileFacts(target),
        },
      ]);
    }

    const seedRevisions = new Set(
      outputEntries
        .map(([, entry]) => entry.seed_revision)
        .filter((value): value is string => typeof value === "string"),
    );
    const manifest = {
      schema_version: 2,
      generated_at: generatedAt,
      seed_profile: "screenshots",
      seed_revision: migrateLegacy
        ? "legacy-pre-catalog"
        : seedRevisions.size === 1
          ? [...seedRevisions][0]
          : null,
      source_commit: isCompleteMatrix ? sourceCommit : null,
      source_worktree_dirty: isCompleteMatrix ? this.sourceWorktreeDirty : null,
      projects: runProjects,
      provenance: migrateLegacy
        ? "legacy-migration"
        : isCompleteMatrix
          ? "current-run"
          : "partial-run",
      entries: sortedRecord(outputEntries),
    };

    fs.mkdirSync(path.dirname(MANIFEST_PATH), { recursive: true });
    const temporary = `${MANIFEST_PATH}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(manifest, null, 2)}\n`);
    fs.renameSync(temporary, MANIFEST_PATH);
    console.log(`[manifest] ✓ 重建 ${outputEntries.length} 项：${MANIFEST_PATH}`);
  }
}
