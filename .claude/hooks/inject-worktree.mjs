#!/usr/bin/env node

const READ_ONLY_SUBAGENTS = new Set(["Explore", "Plan", "claude-code-guide", "statusline-setup"]);

function emit(output) {
  process.stdout.write(JSON.stringify(output));
  process.exit(0);
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
  const subagentType = toolInput.subagent_type;

  if (toolInput.isolation === "worktree" || READ_ONLY_SUBAGENTS.has(subagentType)) {
    emit({});
  }

  emit({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: `Forcing isolation: "worktree" for subagent "${subagentType ?? "unknown"}" (project policy).`,
      updatedInput: { ...toolInput, isolation: "worktree" },
    },
  });
});
