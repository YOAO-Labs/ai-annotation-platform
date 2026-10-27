import { useCallback, useId, useLayoutEffect, useRef } from "react";
import { projectsApi, type GuideAssetEntry } from "@/api/projects";
import type { MarkdownImageResolver } from "@/components/markdown/types";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

const IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/svg+xml",
]);
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

type ImageAddress = { url: string };
interface ImageOwner {
  identity: string;
  userId: string | null;
  sessionId: string | null;
  projectId: string | undefined;
  active: boolean;
  epoch: number;
  controllers: Set<AbortController>;
  addresses: Set<string>;
  cache: Map<string, ImageAddress>;
  inFlight: Map<string, Promise<ImageAddress>>;
  keyVersions: Map<string, number>;
}

function changedContext(): Error {
  return new Error("指南图片会话已变更，请在当前项目重试");
}

function retire(owner: ImageOwner) {
  owner.active = false;
  owner.epoch += 1;
  for (const controller of owner.controllers) controller.abort();
  owner.controllers.clear();
  for (const address of owner.addresses) {
    if (address.startsWith("blob:")) URL.revokeObjectURL(address);
  }
  owner.addresses.clear();
  owner.cache.clear();
  owner.inFlight.clear();
  owner.keyVersions.clear();
}

/** Preserve SVG bytes in an opaque-origin data URL, never a same-origin Blob document. */
function svgAddress(blob: Blob, signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => reader.abort();
    const cleanup = () => signal.removeEventListener("abort", abort);
    reader.onload = () => {
      cleanup();
      if (signal.aborted || typeof reader.result !== "string") reject(changedContext());
      else resolve(reader.result);
    };
    reader.onerror = () => {
      cleanup();
      reject(new Error("无法读取指南图片，请重试"));
    };
    reader.onabort = () => {
      cleanup();
      reject(changedContext());
    };
    if (signal.aborted) return reject(changedContext());
    signal.addEventListener("abort", abort, { once: true });
    reader.readAsDataURL(blob);
  });
}

/** One mounted login/project owner retains every image address until its scope retires. */
export function useGuideAssets(projectId: string | undefined) {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const sessionId = useAuthStore((state) => state.sessionId);
  const instanceId = useId();
  const identity = JSON.stringify([instanceId, userId, sessionId, projectId]);
  const ownerRef = useRef<ImageOwner | null>(null);
  if (ownerRef.current?.identity !== identity) {
    // Fence late work during render, before child effects can resolve a new image.
    if (ownerRef.current) {
      ownerRef.current.active = false;
      ownerRef.current.epoch += 1;
    }
    ownerRef.current = {
      identity,
      userId,
      sessionId,
      projectId,
      active: true,
      epoch: 0,
      controllers: new Set(),
      addresses: new Set(),
      cache: new Map(),
      inFlight: new Map(),
      keyVersions: new Map(),
    };
  }
  const owner = ownerRef.current;
  useLayoutEffect(() => {
    // Become live before child preview effects, including StrictMode's second setup.
    // Old requests retain the earlier epoch after cleanup.
    owner.active = true;
    return () => retire(owner);
  }, [owner]);

  const isCurrent = useCallback(() => {
    const auth = useAuthStore.getState();
    return (
      ownerRef.current === owner &&
      owner.active &&
      !!owner.projectId &&
      !!owner.userId &&
      !!owner.sessionId &&
      auth.sessionId === owner.sessionId &&
      isCurrentAuthOwner(owner.userId)
    );
  }, [owner]);

  const assertCurrent = useCallback(
    (epoch = owner.epoch) => {
      if (!isCurrent() || epoch !== owner.epoch) throw changedContext();
    },
    [isCurrent, owner],
  );

  const resolveAddress = useCallback(
    async (key: string, options?: { refresh?: boolean }): Promise<ImageAddress> => {
      assertCurrent();
      if (!key) throw new Error("指南图片缺少资源标识");
      const cached = owner.cache.get(key);
      if (!options?.refresh && cached) return cached;
      const existing = owner.inFlight.get(key);
      if (existing) return existing;
      const epoch = owner.epoch;
      const version = owner.keyVersions.get(key) ?? 0;
      const controller = new AbortController();
      owner.controllers.add(controller);
      const request = (async () => {
        try {
          const blob = await projectsApi.guideAssets.content(owner.projectId!, key, {
            signal: controller.signal,
          });
          assertCurrent(epoch);
          if (version !== (owner.keyVersions.get(key) ?? 0)) throw changedContext();
          if (!IMAGE_TYPES.has(blob.type) || blob.size < 1 || blob.size > MAX_IMAGE_BYTES) {
            throw new Error("指南图片内容不符合要求，请重新上传");
          }
          const svg = blob.type === "image/svg+xml";
          const url = svg ? await svgAddress(blob, controller.signal) : URL.createObjectURL(blob);
          // SVG conversion is asynchronous. A retired result must not escape or enter a cache.
          if (
            !isCurrent() ||
            epoch !== owner.epoch ||
            version !== (owner.keyVersions.get(key) ?? 0)
          ) {
            if (!svg) URL.revokeObjectURL(url);
            throw changedContext();
          }
          owner.addresses.add(url);
          const entry = { url };
          owner.cache.set(key, entry);
          return entry;
        } catch (error) {
          assertCurrent(epoch);
          throw error;
        } finally {
          owner.controllers.delete(controller);
        }
      })();
      owner.inFlight.set(key, request);
      const clear = () => {
        if (owner.inFlight.get(key) === request) owner.inFlight.delete(key);
      };
      void request.then(clear, clear);
      return request;
    },
    [assertCurrent, isCurrent, owner],
  );

  const uploadAsset = useCallback(
    async (file: File): Promise<{ src: string; alt: string; asset: GuideAssetEntry }> => {
      assertCurrent();
      if (!IMAGE_TYPES.has(file.type)) {
        throw new Error("请选择 PNG、JPEG、WebP、GIF 或 SVG 图片");
      }
      if (file.size < 1 || file.size > MAX_IMAGE_BYTES) {
        throw new Error("指南图片须大于 0 且不超过 5MiB");
      }
      const epoch = owner.epoch;
      const controller = new AbortController();
      owner.controllers.add(controller);
      try {
        const asset = await projectsApi.guideAssets.upload(owner.projectId!, file, {
          signal: controller.signal,
        });
        assertCurrent(epoch);
        return { src: `guide-asset:${asset.key}`, alt: asset.original_name, asset };
      } catch (error) {
        assertCurrent(epoch);
        throw error;
      } finally {
        owner.controllers.delete(controller);
      }
    },
    [assertCurrent, owner],
  );

  const deleteAsset = useCallback(
    async (key: string) => {
      assertCurrent();
      const epoch = owner.epoch;
      const controller = new AbortController();
      owner.controllers.add(controller);
      try {
        await projectsApi.guideAssets.remove(owner.projectId!, key, { signal: controller.signal });
        assertCurrent(epoch);
        owner.keyVersions.set(key, (owner.keyVersions.get(key) ?? 0) + 1);
        owner.cache.delete(key);
        owner.inFlight.delete(key);
        // Other images and an enlarged preview may still use an earlier address.
      } catch (error) {
        assertCurrent(epoch);
        throw error;
      } finally {
        owner.controllers.delete(controller);
      }
    },
    [assertCurrent, owner],
  );

  const signAsset = useCallback(
    async (key: string, options?: { refresh?: boolean }): Promise<string> =>
      (await resolveAddress(key, options)).url,
    [resolveAddress],
  );
  const resolveImage = useCallback<MarkdownImageResolver>(
    async (src, options) => {
      if (!src.startsWith("guide-asset:")) throw new Error("仅支持解析指南图片资源");
      return resolveAddress(src.slice("guide-asset:".length), options);
    },
    [resolveAddress],
  );

  return { uploadAsset, deleteAsset, signAsset, resolveImage, imageScope: identity, isCurrent };
}
