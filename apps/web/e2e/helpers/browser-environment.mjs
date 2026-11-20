// Browser processes do not need API, database, storage, or fixture authority.
const allowed = {
  PATH: true,
  HOME: true,
  USER: true,
  LOGNAME: true,
  TMPDIR: true,
  TMP: true,
  TEMP: true,
  LANG: true,
  LC_ALL: true,
  LC_CTYPE: true,
  TZ: true,
  XDG_RUNTIME_DIR: true,
  XDG_CACHE_HOME: true,
  FONTCONFIG_PATH: true,
  XAUTHORITY: true,
  DBUS_SESSION_BUS_ADDRESS: true,
  LD_LIBRARY_PATH: true,
  DISPLAY: true,
};

export function selectedWorktreeMode(env = process.env) {
  if (
    Object.keys(env).some(
      (key) =>
        key.toUpperCase() === "AAP_WORKTREE_MODE" ||
        (key.toUpperCase() === "YOAO_WORKTREE_MODE" && key !== "YOAO_WORKTREE_MODE"),
    )
  ) {
    throw new Error("Remove the legacy or ambiguous worktree mode before launching a browser.");
  }
  const mode = env.YOAO_WORKTREE_MODE;
  if (mode !== undefined && !["dev", "test", "e2e"].includes(mode)) {
    throw new Error("Invalid YOAO_WORKTREE_MODE.");
  }
  return mode;
}

/** @returns {Record<string, string>} */
export function browserEnvironment({ dropForwardedDisplay = false } = {}) {
  return Object.fromEntries(
    Object.entries(process.env).filter(
      ([key, value]) =>
        value !== undefined &&
        Object.hasOwn(allowed, key) &&
        !(dropForwardedDisplay && key === "DISPLAY" && /^localhost:\d+(?:\.\d+)?$/.test(value)),
    ),
  );
}
