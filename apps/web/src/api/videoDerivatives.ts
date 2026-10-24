import { useAuthStore } from "@/stores/authStore";
import { ApiError } from "./client";

export interface VideoDerivativeIdentity {
  output_id?: string | null;
  sha256?: string | null;
  metadata_generation_id?: string | null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
let credentials: { token: string | null; owner: string; revision: number } | undefined;

/** Public namespace only; credentials and signed URLs never become cache keys. */
export function videoDerivativeOwnerKey(): string | null {
  const auth = useAuthStore.getState();
  const owner = JSON.stringify([auth.user?.id ?? null, auth.sessionId]);
  if (!credentials || credentials.token !== auth.token || credentials.owner !== owner) {
    credentials = { token: auth.token, owner, revision: (credentials?.revision ?? 0) + 1 };
  }
  return auth.user?.id &&
    auth.sessionId &&
    auth.token === localStorage.getItem("token") &&
    auth.token
    ? JSON.stringify([owner, credentials.revision])
    : null;
}

export function useVideoDerivativeOwnerKey(): string | null {
  useAuthStore((state) => state.user?.id);
  useAuthStore((state) => state.sessionId);
  useAuthStore((state) => state.token);
  return videoDerivativeOwnerKey();
}

export function isVideoGeneration(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

export function videoDerivativeKey(value: VideoDerivativeIdentity): string | null {
  return isVideoGeneration(value.output_id) &&
    isVideoGeneration(value.metadata_generation_id) &&
    typeof value.sha256 === "string" &&
    SHA256.test(value.sha256)
    ? JSON.stringify([value.metadata_generation_id, value.output_id, value.sha256])
    : null;
}

export function assertVideoDerivativeOwner(ownerKey: string | null, signal?: AbortSignal): void {
  signal?.throwIfAborted();
  if (!ownerKey || ownerKey !== videoDerivativeOwnerKey()) {
    throw new DOMException("视频登录会话已变更", "AbortError");
  }
}

export function assertVideoSamplesIdentity(
  chunk: VideoDerivativeIdentity,
  samples: VideoDerivativeIdentity,
): void {
  const key = videoDerivativeKey(chunk);
  if (!key || videoDerivativeKey(samples) !== key) {
    throw new Error("视频分块与索引版本不一致，请重新获取");
  }
}

export async function verifyVideoChunkBytes(
  bytes: ArrayBuffer,
  identity: VideoDerivativeIdentity & { byte_size?: number | null },
): Promise<void> {
  if (
    !videoDerivativeKey(identity) ||
    !Number.isSafeInteger(identity.byte_size) ||
    identity.byte_size !== bytes.byteLength
  ) {
    throw new Error("视频分块内容长度或身份无效");
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  if (sha256 !== identity.sha256) throw new Error("视频分块内容校验失败");
}

export function retryPreparingVideoMetadata(failureCount: number, error: unknown): boolean {
  const detail = error instanceof ApiError ? error.detailRaw : null;
  return (
    failureCount < 10 &&
    error instanceof ApiError &&
    error.status === 503 &&
    !!detail &&
    typeof detail === "object" &&
    "code" in detail &&
    detail.code === "video_metadata_preparing"
  );
}

export function videoMetadataRetryDelay(_attempt: number, error: unknown): number {
  const seconds = error instanceof ApiError ? Number(error.headers?.["retry-after"]) : NaN;
  return Number.isFinite(seconds) && seconds > 0
    ? Math.min(10_000, Math.max(1_000, seconds * 1_000))
    : 2_000;
}
