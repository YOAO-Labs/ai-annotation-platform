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

/** @returns {Record<string, string>} */
export function browserEnvironment({ dropForwardedDisplay = false } = {}) {
  const environment = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key, value]) =>
        value !== undefined &&
        Object.hasOwn(allowed, key) &&
        !(dropForwardedDisplay && key === "DISPLAY" && /^localhost:\d+(?:\.\d+)?$/.test(value)),
    ),
  );
  if (
    process.platform === "linux" &&
    process.env.YOAO_WORKTREE_MODE === "e2e" &&
    process.env.PLAYWRIGHT_CHROMIUM_CHANNEL === "chrome"
  ) {
    environment.TMPDIR = "/tmp";
  }
  return environment;
}
