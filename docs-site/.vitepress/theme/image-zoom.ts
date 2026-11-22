export function setupImageZoom(): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  if ((window as unknown as { __docsImageZoom?: boolean }).__docsImageZoom) return;
  (window as unknown as { __docsImageZoom?: boolean }).__docsImageZoom = true;

  let overlay: HTMLDivElement | null = null;

  const close = (): void => {
    if (!overlay) return;
    overlay.classList.remove("open");
    overlay.replaceChildren();
    document.documentElement.style.overflow = "";
  };

  const ensureOverlay = (): HTMLDivElement => {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.className = "docs-image-lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "图片放大预览");
    overlay.addEventListener("click", close);
    document.body.appendChild(overlay);
    return overlay;
  };

  const open = (src: string, alt: string, paper = false): void => {
    const o = ensureOverlay();
    o.replaceChildren();
    const img = document.createElement("img");
    img.src = src;
    img.alt = alt;
    if (paper) img.classList.add("docs-image-lightbox-paper");
    o.appendChild(img);
    o.classList.add("open");
    document.documentElement.style.overflow = "hidden";
  };

  document.addEventListener("click", (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!(target instanceof HTMLImageElement)) return;
    if (!target.closest(".vp-doc")) return;
    if (target.closest("a")) return;
    if (target.classList.contains("no-zoom")) return;
    if (target.naturalWidth && target.naturalWidth < 80) return;
    e.preventDefault();
    open(
      target.currentSrc || target.src,
      target.alt || "",
      Boolean(target.closest(".excalidraw-diagram")),
    );
  });

  document.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Escape") close();
  });
}
