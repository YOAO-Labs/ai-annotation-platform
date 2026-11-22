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

export function avatarInitial(
  name?: string | null,
  email?: string | null,
  explicit?: string | null,
): string {
  const source = (explicit || name || email || "?").trim();
  return source ? source.slice(0, 1).toUpperCase() : "?";
}
