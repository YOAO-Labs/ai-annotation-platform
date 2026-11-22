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
