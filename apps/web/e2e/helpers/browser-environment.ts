// Browser processes do not need API, database, storage, or fixture authority.
const allowed: Record<string, true> = {
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

export function browserEnvironment({ dropForwardedDisplay = false } = {}): Record<string, string> {
  return Object.fromEntries(
    Object.entries(process.env).filter(
      (entry): entry is [string, string] =>
        entry[1] !== undefined &&
        allowed[entry[0]] === true &&
        !(
          dropForwardedDisplay &&
          entry[0] === "DISPLAY" &&
          /^localhost:\d+(?:\.\d+)?$/.test(entry[1])
        ),
    ),
  );
}
