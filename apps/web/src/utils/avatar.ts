/**
 * 头像引用的前端解析（配套后端 `services/avatar.py` 的语法）。
 *
 * 后端只在用户身份 payload 里回传一个 `avatar_ref`，不返回 URL：URL 是引用的纯函数，
 * 由前端在一处还原，避免在十几个端点里回填签名 URL（见开发计划 §3.4）。
 *
 * - `preset:<slug>` → 随前端产物发布的静态 SVG（`public/avatars/pixel/`），可长期缓存。
 * - `upload:<32位hex>` → 后端能力 URL（`GET /api/v1/avatars/{token}`），免鉴权、强缓存。
 * - 其它/空 → null，调用方回退首字母。
 *
 * 语法校验与后端同源：畸形引用一律返回 null，绝不把任意字符串拼进 `<img src>`。
 */

export const AVATAR_PRESET_PREFIX = "preset:";
export const AVATAR_UPLOAD_PREFIX = "upload:";
export const AVATAR_PRESET_DIR = "/avatars/pixel";

const PRESET_SLUG_RE = /^[a-z0-9-]{1,40}$/;
const UPLOAD_TOKEN_RE = /^[0-9a-f]{32}$/;

export function presetAvatarUrl(slug: string): string {
  return `${AVATAR_PRESET_DIR}/${slug}.svg`;
}

export function resolveAvatarUrl(ref: string | null | undefined): string | null {
  if (!ref) return null;
  if (ref.startsWith(AVATAR_PRESET_PREFIX)) {
    const slug = ref.slice(AVATAR_PRESET_PREFIX.length);
    return PRESET_SLUG_RE.test(slug) ? presetAvatarUrl(slug) : null;
  }
  if (ref.startsWith(AVATAR_UPLOAD_PREFIX)) {
    const token = ref.slice(AVATAR_UPLOAD_PREFIX.length);
    return UPLOAD_TOKEN_RE.test(token) ? `/api/v1/avatars/${token}` : null;
  }
  return null;
}

/** 首字母回退：优先用后端给的 `avatar_initial`，否则由姓名/邮箱推导。 */
export function avatarInitial(
  name?: string | null,
  email?: string | null,
  explicit?: string | null,
): string {
  const source = (explicit || name || email || "?").trim();
  return source ? source.slice(0, 1).toUpperCase() : "?";
}
