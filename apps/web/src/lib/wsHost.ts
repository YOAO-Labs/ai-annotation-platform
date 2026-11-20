/**
 * WS host / URL helper.
 *
 * 收口 4 处 hook 重复的 host 拼接逻辑 (useNotificationSocket / useGlobalPreannotationJobs /
 * usePreannotation / PerfHud/useMLBackendStats):
 *
 *   const proto = window.location.protocol === "https:" ? "wss" : "ws";
 *   const host = import.meta.env.VITE_WS_HOST || window.location.host;
 *   const url = `${proto}://${host}/ws/...?token=${encodeURIComponent(t)}`;
 *
 * DEV 与 production 都默认走页面同源 `/ws` 代理。这样本机、LAN 与 SSH LocalForward
 * 访问使用同一条路径，不会把访问端的 `localhost` 错当成 API 所在主机。
 *
 * DEV 需要刻意绕过 Vite proxy 时仍可用 `VITE_WS_HOST` 覆盖。
 */

type WsLocation = Pick<Location, "host">;

export function getWsHost(
  location: WsLocation = window.location,
  configuredHost: string | undefined = import.meta.env.VITE_WS_HOST,
): string {
  if (import.meta.env.DEV && configuredHost) return configuredHost;
  return location.host;
}

export function getWsProtocol(): "ws" | "wss" {
  return window.location.protocol === "https:" ? "wss" : "ws";
}

/**
 * 拼一个完整 ws://host/path?k=v 的 URL.
 *
 * - path 必须以 "/" 开头 (例: "/ws/notifications").
 * - params 的值会被 encodeURIComponent.
 */
export function buildWsUrl(
  path: string,
  params?: Record<string, string | undefined | null>,
): string {
  const proto = getWsProtocol();
  const host = getWsHost();
  let url = `${proto}://${host}${path}`;
  if (params) {
    const qs: string[] = [];
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue;
      qs.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    if (qs.length) url += (path.includes("?") ? "&" : "?") + qs.join("&");
  }
  return url;
}
