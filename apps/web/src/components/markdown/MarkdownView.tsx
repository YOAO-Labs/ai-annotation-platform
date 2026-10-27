import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import ReactMarkdown, {
  defaultUrlTransform,
  type Components,
  type UrlTransform,
} from "react-markdown";
import remarkGfm from "remark-gfm";

import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { cn } from "@/lib/utils";

import type { MarkdownImageResolver } from "./types";
import styles from "./MarkdownView.module.css";

export type { MarkdownImageResolver } from "./types";

export interface MarkdownViewProps {
  content: string;
  compact?: boolean;
  resolveImage?: MarkdownImageResolver;
  /** Logical identity of the document/project owning asynchronous images. */
  imageScope?: string;
  /** Optional owner dialog ref for the image preview portal. */
  modalContainerRef?: RefObject<HTMLElement | null>;
}

type ImageStatus = "loading" | "ready" | "unavailable";
type ImageFailureKind = "forbidden" | "missing" | "unverified" | "unavailable";

interface ImageFailure {
  kind: ImageFailureKind;
  status?: number;
}

interface ResolvedImage {
  url: string;
  expiresAt?: number;
}

interface MarkdownImageProps {
  src?: string;
  alt?: string;
  title?: string;
  resolveImage?: MarkdownImageResolver;
  imageScope?: string;
  onEnlarge: (image: { src: string; alt: string; title?: string }) => void;
}

interface EnlargedImage {
  src: string;
  alt: string;
  title?: string;
  content: string;
  scope?: string;
  resolver?: MarkdownImageResolver;
}

interface MarkdownAstNode {
  type?: string;
  url?: string;
  identifier?: string;
  data?: {
    hProperties?: Record<string, unknown>;
    [key: string]: unknown;
  };
  children?: MarkdownAstNode[];
}

const MarkdownLinkContext = createContext(false);

function isGuideAssetSource(src: string): boolean {
  return src.startsWith("guide-asset:");
}

function walkMarkdownTree(node: MarkdownAstNode, visit: (node: MarkdownAstNode) => void): void {
  visit(node);
  node.children?.forEach((child) => walkMarkdownTree(child, visit));
}

/**
 * mdast-util-to-hast normalizes image URLs (for example, encoding Chinese
 * characters, spaces, and percent signs). Keep the source that belongs to our
 * private guide-asset protocol on the mdast node so the resolver receives the
 * storage key exactly as it was authored. This also covers reference images,
 * whose URL lives on a separate definition node.
 */
function preserveGuideAssetSources() {
  return (tree: MarkdownAstNode) => {
    const definitions = new Map<string, string>();
    walkMarkdownTree(tree, (node) => {
      if (node.type === "definition" && typeof node.identifier === "string" && node.url) {
        if (!definitions.has(node.identifier)) definitions.set(node.identifier, node.url);
      }
    });

    walkMarkdownTree(tree, (node) => {
      const source =
        node.type === "image"
          ? node.url
          : node.type === "imageReference" && node.identifier
            ? definitions.get(node.identifier)
            : undefined;
      if (!source || !isGuideAssetSource(source)) return;
      node.data = {
        ...node.data,
        hProperties: {
          ...node.data?.hProperties,
          src: source,
          "data-guide-asset-source": source,
        },
      };
    });
  };
}

function originalGuideAssetSource(properties: { [key: string]: unknown }): string | undefined {
  const source = properties["data-guide-asset-source"];
  return typeof source === "string" && isGuideAssetSource(source) ? source : undefined;
}

function isExpired(expiresAt?: number): boolean {
  return typeof expiresAt === "number" && expiresAt <= Date.now();
}

function classifyImageFailure(error: unknown): ImageFailure {
  const status =
    error && typeof error === "object" && "status" in error
      ? Number((error as { status?: unknown }).status)
      : undefined;
  if (status === 403) return { kind: "forbidden", status };
  if (status === 404) return { kind: "missing", status };
  const detail =
    error && typeof error === "object" && "detailRaw" in error ? error.detailRaw : undefined;
  if (
    status === 409 &&
    detail &&
    typeof detail === "object" &&
    "code" in detail &&
    detail.code === "guide_asset_storage_unpinned"
  ) {
    return { kind: "unverified", status };
  }
  return { kind: "unavailable", status: Number.isFinite(status) ? status : undefined };
}

function imageFailureLabel(failure: ImageFailure): string {
  if (failure.kind === "forbidden") return "图片无权访问";
  if (failure.kind === "missing") return "图片已不存在";
  if (failure.kind === "unverified") return "图片无法核验，请重新上传";
  return "图片暂时不可用";
}

function textFromNode(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  if (typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: ReactNode } }).props;
    return textFromNode(props?.children);
  }
  return "";
}

function codeLanguage(children: ReactNode): string | null {
  if (!children || typeof children !== "object" || !("props" in children)) return null;
  const className = (children as { props?: { className?: unknown } }).props?.className;
  if (typeof className !== "string") return null;
  const language = className.match(/(?:^|\s)language-([^\s]+)/)?.[1];
  return language || null;
}

function CopyCodeButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
    },
    [],
  );

  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
      } else {
        const textarea = document.createElement("textarea");
        try {
          textarea.value = code;
          textarea.setAttribute("readonly", "true");
          textarea.style.position = "fixed";
          textarea.style.opacity = "0";
          document.body.appendChild(textarea);
          textarea.select();
          const copiedByCommand = document.execCommand("copy");
          if (!copiedByCommand) throw new Error("clipboard unavailable");
        } finally {
          textarea.remove();
        }
      }
      setCopied(true);
      setCopyError(false);
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
      setCopyError(true);
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setCopyError(false), 2000);
    }
  };

  return (
    <button
      type="button"
      className={styles.copyButton}
      onClick={() => void copy()}
      aria-label={copyError ? "复制失败" : "复制代码"}
      title="复制代码"
    >
      <Icon name="copy" size={13} />
      <span className={copied ? styles.copyStatus : undefined}>
        {copied ? "已复制" : copyError ? "复制失败" : "复制"}
      </span>
    </button>
  );
}

function MarkdownCodeBlock({ children }: { children?: ReactNode }) {
  const code = textFromNode(children).replace(/\n$/, "");
  const language = codeLanguage(children);
  return (
    <div className={styles.codeBlock}>
      <div className={styles.codeHeader}>
        <span className={styles.codeLanguage}>{language ?? "代码"}</span>
        <CopyCodeButton code={code} />
      </div>
      <pre className={styles.pre}>{children}</pre>
    </div>
  );
}

function MarkdownImage({
  src,
  alt = "",
  title,
  resolveImage,
  imageScope,
  onEnlarge,
}: MarkdownImageProps) {
  const insideLink = useContext(MarkdownLinkContext);
  const source = typeof src === "string" ? src : "";
  const isGuideAsset = isGuideAssetSource(source);
  const imageOwner = useMemo(
    () => ({ source, imageScope, resolveImage }),
    [imageScope, resolveImage, source],
  );
  const [status, setStatus] = useState<ImageStatus>("loading");
  const [displayState, setDisplayState] = useState(() => ({
    owner: imageOwner,
    url: isGuideAsset ? "" : source,
  }));
  // Keep a stale URL out of the first render after a Markdown source changes;
  // the effect below will populate a new scoped URL asynchronously.
  const isDisplayCurrent = displayState.owner === imageOwner;
  const displaySrc = isDisplayCurrent ? displayState.url : "";
  const [expiresAt, setExpiresAt] = useState<number | undefined>();
  const [failure, setFailure] = useState<ImageFailure>({ kind: "unavailable" });
  const [retryCount, setRetryCount] = useState(0);
  const generationRef = useRef(0);
  const automaticRefreshRef = useRef(false);

  const resolve = useCallback(
    async (refresh = false): Promise<ResolvedImage> => {
      if (!resolveImage) throw new Error("image resolver unavailable");
      const result = refresh
        ? await resolveImage(source, { refresh: true })
        : await resolveImage(source);
      if (!result?.url) throw new Error("image resolver returned an empty URL");
      return result;
    },
    [resolveImage, source],
  );

  const loadGuideImage = useCallback(
    async (refresh: boolean, generation: number) => {
      try {
        let result = await resolve(refresh);
        // Do not mount a URL that the resolver already reports as expired. A
        // forced second lookup is bounded to this load operation.
        if (!refresh && isExpired(result.expiresAt)) result = await resolve(true);
        if (isExpired(result.expiresAt)) throw new Error("resolved image URL is expired");
        if (generation !== generationRef.current) return;
        if (refresh) setRetryCount((count) => count + 1);
        setDisplayState({ owner: imageOwner, url: result.url });
        setExpiresAt(result.expiresAt);
        setFailure({ kind: "unavailable" });
        setStatus("loading");
      } catch (error) {
        if (generation !== generationRef.current) return;
        setFailure(classifyImageFailure(error));
        setStatus("unavailable");
      }
    },
    [imageOwner, resolve],
  );

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    automaticRefreshRef.current = false;
    setRetryCount(0);
    setExpiresAt(undefined);
    setFailure({ kind: "unavailable" });
    if (!source) {
      setDisplayState({ owner: imageOwner, url: "" });
      setStatus("unavailable");
      return () => {
        if (generationRef.current === generation) generationRef.current += 1;
      };
    }
    if (!isGuideAsset) {
      setDisplayState({ owner: imageOwner, url: source });
      setStatus("loading");
      return () => {
        if (generationRef.current === generation) generationRef.current += 1;
      };
    }
    if (!resolveImage) {
      setDisplayState({ owner: imageOwner, url: "" });
      setStatus("unavailable");
      return () => {
        if (generationRef.current === generation) generationRef.current += 1;
      };
    }
    setDisplayState({ owner: imageOwner, url: "" });
    setStatus("loading");
    void loadGuideImage(false, generation);
    return () => {
      if (generationRef.current === generation) generationRef.current += 1;
    };
  }, [imageOwner, imageScope, isGuideAsset, loadGuideImage, resolveImage, source]);

  useEffect(() => {
    if (!isGuideAsset || !resolveImage) return;
    const checkExpiry = () => {
      if (document.visibilityState !== "visible" || !isExpired(expiresAt)) return;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setStatus("loading");
      void loadGuideImage(true, generation);
    };
    document.addEventListener("visibilitychange", checkExpiry);
    return () => document.removeEventListener("visibilitychange", checkExpiry);
  }, [expiresAt, imageScope, isGuideAsset, loadGuideImage, resolveImage]);

  const handleLoad = () => {
    // A signed-URL refresh is considered consumed until the replacement image
    // itself loads successfully. Resolving a URL alone must not re-arm the
    // automatic retry loop when the storage endpoint keeps returning errors.
    automaticRefreshRef.current = false;
    setStatus("ready");
    setFailure({ kind: "unavailable" });
  };

  const handleError = () => {
    if (isGuideAsset && resolveImage && !automaticRefreshRef.current) {
      automaticRefreshRef.current = true;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setStatus("loading");
      void loadGuideImage(true, generation);
      return;
    }
    setFailure({ kind: "unavailable" });
    setStatus("unavailable");
  };

  const handleRetry = () => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setRetryCount((count) => count + 1);
    automaticRefreshRef.current = true;
    setStatus("loading");
    setFailure({ kind: "unavailable" });
    if (isGuideAsset && resolveImage) {
      void loadGuideImage(true, generation);
      return;
    }
    // Remounting is enough to ask the browser to retry while preserving the
    // exact ordinary URL (including query signatures and fragments).
    setDisplayState({ owner: imageOwner, url: source });
  };

  if (source && (!isDisplayCurrent || (!displaySrc && status === "loading"))) {
    return (
      <span
        className={styles.imageState}
        role="status"
        aria-label="加载图片中"
        data-testid="markdown-image-loading"
      >
        加载图片中…
      </span>
    );
  }

  if (status === "unavailable" || !displaySrc) {
    return (
      <span
        className={cn(styles.imageState, styles.imageError)}
        role="status"
        aria-label={imageFailureLabel(failure)}
        data-testid="markdown-image-error"
      >
        <span>{imageFailureLabel(failure)}</span>
        {!insideLink && source && (
          <button
            type="button"
            className={styles.imageRetry}
            onClick={handleRetry}
            data-testid="markdown-image-retry"
          >
            重试
          </button>
        )}
      </span>
    );
  }

  const image = (
    <img
      key={`${displaySrc}:${retryCount}`}
      src={displaySrc}
      alt={alt}
      title={title}
      className={cn(styles.image, status === "loading" && styles.imageLoading)}
      onLoad={handleLoad}
      onError={handleError}
      aria-busy={status === "loading" ? "true" : undefined}
      data-testid={status === "loading" ? "markdown-image-loading" : "markdown-image-ready"}
    />
  );

  if (insideLink) {
    return (
      <span className={styles.imageLinked} data-testid="markdown-image">
        {image}
      </span>
    );
  }

  return (
    <button
      type="button"
      className={styles.imageButton}
      onClick={() => onEnlarge({ src: displaySrc, alt, title })}
      aria-label={alt ? `放大图片：${alt}` : "放大图片"}
      data-testid="markdown-image"
    >
      {image}
    </button>
  );
}

function createUrlTransform(resolveImage?: MarkdownImageResolver): UrlTransform {
  return (url, key, node) => {
    // react-markdown's default transform intentionally rejects unknown
    // protocols. Only image src values with an authorized resolver may retain
    // this private scheme; hrefs and every other URL remain default-filtered.
    if (key === "src" && node.tagName === "img" && resolveImage) {
      const originalSource = originalGuideAssetSource(node.properties);
      if (originalSource) return originalSource;
      if (isGuideAssetSource(url)) return url;
    }
    return defaultUrlTransform(url);
  };
}

export function MarkdownView({
  content,
  compact = false,
  resolveImage,
  imageScope,
  modalContainerRef,
}: MarkdownViewProps) {
  const [enlargedImage, setEnlargedImage] = useState<EnlargedImage | null>(null);
  const imageCacheRef = useRef<Map<string, ResolvedImage>>(new Map());
  const imageInFlightRef = useRef<Map<string, Promise<ResolvedImage>>>(new Map());
  const imageCacheEpochRef = useRef(0);
  useEffect(
    () => () => {
      // StrictMode replays child image effects without discarding these refs.
      // Never let that second load inherit the hook owner's cancelled first request.
      imageCacheEpochRef.current += 1;
      imageCacheRef.current.clear();
      imageInFlightRef.current.clear();
    },
    [],
  );
  const imageCacheOwnerRef = useRef<{
    scope?: string;
    resolver?: MarkdownImageResolver;
  }>({ scope: imageScope, resolver: resolveImage });
  if (
    imageCacheOwnerRef.current.scope !== imageScope ||
    imageCacheOwnerRef.current.resolver !== resolveImage
  ) {
    imageCacheOwnerRef.current = { scope: imageScope, resolver: resolveImage };
    imageCacheRef.current.clear();
    imageInFlightRef.current.clear();
  }
  const scopedResolveImage = useMemo<MarkdownImageResolver | undefined>(() => {
    if (!resolveImage) return undefined;
    return async (src, options) => {
      const cacheKey = `${imageScope ?? ""}\u0000${src}`;
      const refresh = options?.refresh === true;
      const cached = imageCacheRef.current.get(cacheKey);
      if (!refresh && cached && !isExpired(cached.expiresAt)) return cached;
      const existing = imageInFlightRef.current.get(cacheKey);
      if (existing) return existing;

      const ownerResolver = resolveImage;
      const ownerScope = imageScope;
      const epoch = imageCacheEpochRef.current;
      const request = (options ? resolveImage(src, options) : resolveImage(src)).then((result) => {
        if (
          result?.url &&
          epoch === imageCacheEpochRef.current &&
          imageCacheOwnerRef.current.resolver === ownerResolver &&
          imageCacheOwnerRef.current.scope === ownerScope
        ) {
          imageCacheRef.current.set(cacheKey, result);
        }
        return result;
      });
      imageInFlightRef.current.set(cacheKey, request);
      const clearInFlight = () => {
        if (imageInFlightRef.current.get(cacheKey) === request) {
          imageInFlightRef.current.delete(cacheKey);
        }
      };
      request.then(clearInFlight, clearInFlight);
      return request;
    };
  }, [imageScope, resolveImage]);
  const contentRef = useRef(content);
  const imageScopeRef = useRef(imageScope);
  const resolveImageRef = useRef(scopedResolveImage);
  contentRef.current = content;
  imageScopeRef.current = imageScope;
  resolveImageRef.current = scopedResolveImage;
  const onEnlarge = useCallback(
    (image: Omit<EnlargedImage, "content" | "scope">) =>
      setEnlargedImage({
        ...image,
        content: contentRef.current,
        scope: imageScopeRef.current,
        resolver: resolveImageRef.current,
      }),
    [],
  );

  useEffect(() => {
    setEnlargedImage(null);
  }, [content, imageScope, resolveImage]);

  const visibleEnlargedImage =
    enlargedImage &&
    enlargedImage.content === content &&
    enlargedImage.scope === imageScope &&
    enlargedImage.resolver === scopedResolveImage
      ? enlargedImage
      : null;

  const components = useMemo<Components>(
    () => ({
      h1: ({ children, node: _node, ...props }) => <h1 {...props}>{children}</h1>,
      h2: ({ children, node: _node, ...props }) => <h2 {...props}>{children}</h2>,
      h3: ({ children, node: _node, ...props }) => <h3 {...props}>{children}</h3>,
      h4: ({ children, node: _node, ...props }) => <h4 {...props}>{children}</h4>,
      h5: ({ children, node: _node, ...props }) => <h5 {...props}>{children}</h5>,
      h6: ({ children, node: _node, ...props }) => <h6 {...props}>{children}</h6>,
      p: ({ children, node: _node, ...props }) => (
        <p className={styles.paragraph} {...props}>
          {children}
        </p>
      ),
      a: ({ href, children, node: _node, ...props }) => (
        <a href={href} target="_blank" rel="noopener noreferrer" className={styles.link} {...props}>
          <MarkdownLinkContext.Provider value={true}>{children}</MarkdownLinkContext.Provider>
        </a>
      ),
      strong: ({ children, node: _node, ...props }) => (
        <strong className={styles.strong} {...props}>
          {children}
        </strong>
      ),
      em: ({ children, node: _node, ...props }) => (
        <em className={styles.emphasis} {...props}>
          {children}
        </em>
      ),
      ul: ({ children, className, node: _node, ...props }) => (
        <ul className={cn(styles.list, className)} {...props}>
          {children}
        </ul>
      ),
      ol: ({ children, className, node: _node, ...props }) => (
        <ol className={cn(styles.list, className)} {...props}>
          {children}
        </ol>
      ),
      li: ({ children, className, node: _node, ...props }) => (
        <li className={cn(styles.listItem, className)} {...props}>
          {children}
        </li>
      ),
      blockquote: ({ children, node: _node, ...props }) => (
        <blockquote className={styles.blockquote} {...props}>
          {children}
        </blockquote>
      ),
      code: ({ children, className }) => (
        <code className={className ? cn(styles.inlineCode, className) : styles.inlineCode}>
          {children}
        </code>
      ),
      pre: ({ children }) => <MarkdownCodeBlock>{children}</MarkdownCodeBlock>,
      table: ({ children, node: _node, ...props }) => (
        <div className={styles.tableWrap}>
          <table className={styles.table} {...props}>
            {children}
          </table>
        </div>
      ),
      th: ({ children, node: _node, ...props }) => (
        <th className={styles.tableHeader} {...props}>
          {children}
        </th>
      ),
      td: ({ children, node: _node, ...props }) => (
        <td className={styles.tableCell} {...props}>
          {children}
        </td>
      ),
      section: ({ children, className, node: _node, ...props }) => (
        <section className={cn(styles.footnote, className)} {...props}>
          {children}
        </section>
      ),
      hr: ({ node: _node, ...props }) => <hr className={styles.horizontalRule} {...props} />,
      img: ({ src, alt, title, ...props }) => (
        <MarkdownImage
          src={originalGuideAssetSource(props) ?? src}
          alt={alt}
          title={title}
          resolveImage={scopedResolveImage}
          imageScope={imageScope}
          onEnlarge={onEnlarge}
        />
      ),
    }),
    [imageScope, onEnlarge, scopedResolveImage],
  );

  return (
    <>
      <div className={styles.root} data-compact={compact ? "true" : undefined}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm, preserveGuideAssetSources]}
          skipHtml
          urlTransform={createUrlTransform(scopedResolveImage)}
          components={components}
        >
          {content}
        </ReactMarkdown>
      </div>
      <Modal
        open={visibleEnlargedImage !== null}
        onClose={() => setEnlargedImage(null)}
        title={visibleEnlargedImage?.alt || "图片预览"}
        width={720}
        containerRef={modalContainerRef}
        stopEscapePropagation={modalContainerRef !== undefined}
      >
        {visibleEnlargedImage && (
          <>
            <img
              src={visibleEnlargedImage.src}
              alt={visibleEnlargedImage.alt}
              className={styles.imageModal}
            />
            {visibleEnlargedImage.title && (
              <p className={styles.imageModalCaption}>{visibleEnlargedImage.title}</p>
            )}
          </>
        )}
      </Modal>
    </>
  );
}

export default MarkdownView;
