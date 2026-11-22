export function worktreeMode(environment = process.env) {
  if (
    Object.keys(environment).some(
      (key) =>
        /^(?:AAP_WORKTREE_MODE|AAP_WORKTREE_RESOURCE_ROOT)$/i.test(key) ||
        (key.toUpperCase() === "YOAO_WORKTREE_MODE" && key !== "YOAO_WORKTREE_MODE"),
    )
  ) {
    throw new Error(
      "Remove the legacy or ambiguous worktree environment before launching a browser.",
    );
  }
  const mode = environment.YOAO_WORKTREE_MODE;
  if (mode !== undefined && !["dev", "test", "e2e"].includes(mode)) {
    throw new Error("Invalid YOAO_WORKTREE_MODE.");
  }
  return mode;
}
