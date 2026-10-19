/**
 * 生成随机 UUID v4。
 *
 * `crypto.randomUUID()` 仅在安全上下文（HTTPS / localhost）+ 较新浏览器可用；
 * 通过 LAN IP 走 http 访问时它是 undefined，裸调用会抛 TypeError。
 * 这里优先用它，否则回退到 `crypto.getRandomValues`（非安全上下文也可用），
 * 最后再退到 Math.random，始终保留 UUID v4 格式。仅用于对象与请求标识，
 * 不用于认证凭据或其他安全令牌。
 */
export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
