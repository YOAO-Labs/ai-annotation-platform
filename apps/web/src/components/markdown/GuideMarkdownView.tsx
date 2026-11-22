import { useMemo, type RefObject } from "react";

import MarkdownView from "./MarkdownView";
import type { MarkdownImageResolver } from "./types";

interface GuideMarkdownViewProps {
  content: string;
  resolveAssetUrl?: (
    key: string,
    options?: { refresh?: boolean },
  ) => Promise<string | { url: string; expiresAt?: number }>;
  resolveImage?: MarkdownImageResolver;
  imageScope?: string;
  modalContainerRef?: RefObject<HTMLElement | null>;
}

export function GuideMarkdownView({
  content,
  resolveAssetUrl,
  resolveImage: providedResolver,
  imageScope,
  modalContainerRef,
}: GuideMarkdownViewProps) {
  const resolveImage = useMemo<MarkdownImageResolver | undefined>(() => {
    if (providedResolver) return providedResolver;
    if (!resolveAssetUrl) return undefined;
    return async (src, options) => {
      if (!src.startsWith("guide-asset:")) {
        throw new Error("仅支持解析 guide-asset 图片资源");
      }
      const key = src.slice("guide-asset:".length);
      if (!key) throw new Error("guide-asset 图片缺少资源标识");
      const result = options ? await resolveAssetUrl(key, options) : await resolveAssetUrl(key);
      return typeof result === "string" ? { url: result } : result;
    };
  }, [providedResolver, resolveAssetUrl]);

  return (
    <MarkdownView
      content={content}
      resolveImage={resolveImage}
      imageScope={imageScope}
      modalContainerRef={modalContainerRef}
    />
  );
}

export default GuideMarkdownView;
