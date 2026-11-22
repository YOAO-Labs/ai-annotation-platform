#!/usr/bin/env node

import { resolve, sep } from "node:path";
import { appendFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const LOG = join(tmpdir(), "claude-worktree-guard.log");

function log(line) {
  try {
    appendFileSync(LOG, `${new Date().toISOString()} ${line}\n`);
  } catch {}
}

function emit(output) {
  process.stdout.write(JSON.stringify(output));
  process.exit(0);
}

function deny(reason) {
  log(`DENY ${reason}`);
  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: reason,
    },
  });
}

function isInside(child, parent) {
  if (child === parent) return true;
  return child.startsWith(parent.endsWith(sep) ? parent : parent + sep);
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  raw += chunk;
});
process.stdin.on("end", () => {
  let event;
  try {
    event = JSON.parse(raw || "{}");
  } catch {
    emit({});
  }

  const toolInput = event.tool_input ?? {};
  const toolName = event.tool_name ?? "";
  const cwd = event.cwd;

  if (!cwd) {
    log(`SKIP missing cwd tool=${toolName}`);
    emit({});
  }

  const cwdAbs = resolve(cwd);

  const WORKTREE_MARKER = `${sep}.claude${sep}worktrees${sep}`;
  if (!cwdAbs.includes(WORKTREE_MARKER)) {
    emit({});
  }

  const filePath = toolInput.file_path ?? toolInput.notebook_path;

  if (!filePath) {
    log(`SKIP missing path tool=${toolName} cwd=${cwdAbs}`);
    emit({});
  }

  const targetAbs = resolve(cwd, filePath);

  if (!isInside(targetAbs, cwdAbs)) {
    deny(
      `write target escapes working directory.\n` +
        `  target: ${targetAbs}\n` +
        `  cwd:    ${cwdAbs}\n` +
        `Rewrite the path to stay inside the working directory (use a relative path).`,
    );
  }

  log(`ALLOW ${targetAbs} (cwd=${cwdAbs})`);
  emit({});
});
