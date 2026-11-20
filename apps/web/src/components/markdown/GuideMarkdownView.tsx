import { useMemo, type RefObject } from "react";

import MarkdownView from "./MarkdownView";
import type { MarkdownImageResolver } from "./types";

interface GuideMarkdownViewProps {
  /** Markdown 原文. */
  content: string;
  /** 将 guide-asset:KEY 形式的 src 解析为签名 URL. */
  resolveAssetUrl?: (
    key: string,
    options?: { refresh?: boolean },
  ) => Promise<string | { url: string; expiresAt?: number }>;
  /** New resolver contract; when present it preserves signed URL expiry metadata. */
  resolveImage?: MarkdownImageResolver;
  /** 项目/文档身份，用于隔离图片解析的异步回调. */
  imageScope?: string;
  /** Mount image previews in the owning guide dialog. */
  modalContainerRef?: RefObject<HTMLElement | null>;
}

/**
 * 渲染 annotation guide Markdown.
 *
 * 与 MarkdownBlock 同样基于 react-markdown + remark-gfm, 额外:
 * - <img src="guide-asset:KEY"> 经 resolveAssetUrl 转签名 URL 渲染.
 * - 解析失败 / 加载中显示占位文本, 避免 404.
 */
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
