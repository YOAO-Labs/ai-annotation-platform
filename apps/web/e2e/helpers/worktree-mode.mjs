// A legacy shell must not fall through to the standalone CI fixture defaults.
export function worktreeMode(environment = process.env) {
  if (
    Object.keys(environment).some((key) =>
      /^(?:AAP_WORKTREE_MODE|AAP_WORKTREE_RESOURCE_ROOT)$/i.test(key),
    )
  ) {
    throw new Error("Legacy worktree environment; open a clean shell and use dev:worktree.");
  }
  return environment.YOAO_WORKTREE_MODE;
}
