import { useLayoutEffect, useRef, type RefObject } from "react";

export function pageTableHeaderOffset(
  pageTop: number,
  tableTop: number,
  tableHeight: number,
  headerHeight: number,
): number {
  return Math.max(0, Math.min(pageTop - tableTop, tableHeight - headerHeight));
}

/** Keep one native header aligned with its rows inside a horizontal scroller.
 * CSS sticky cannot follow the outer page through an overflow-x:auto ancestor.
 * Only the vertical offset is compensated here; horizontal scrolling is native.
 */
export function usePageTableHeader<Header extends HTMLElement = HTMLDivElement>(
  pageRef: RefObject<HTMLDivElement | null>,
  tableRef: RefObject<HTMLDivElement | null>,
) {
  // The header can mount after schema loading, or after leaving gallery mode.
  const headerRef = useRef<Header | null>(null);
  useLayoutEffect(() => {
    const header = headerRef.current;
    const page = pageRef.current;
    const table = tableRef.current;
    if (!page || !table || !header) return;
    const originalTransform = header.style.transform;
    let frame: number | null = null;
    const measure = () => {
      const offset = pageTableHeaderOffset(
        page.getBoundingClientRect().top + page.clientTop,
        table.getBoundingClientRect().top + table.clientTop,
        table.clientHeight,
        header.offsetHeight,
      );
      header.style.transform = `translateY(${offset}px)`;
    };
    const schedule = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        measure();
      });
    };
    measure();
    page.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(page);
    observer?.observe(table);
    observer?.observe(header);
    // Summaries and filter controls can move the table without resizing it.
    for (const child of page.children) observer?.observe(child);
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      page.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      observer?.disconnect();
      header.style.transform = originalTransform;
    };
    // Rebind after commits where loading/gallery swaps the DOM. Ref callbacks
    // must not set React state: that can flush the owner's URL hydration early.
  });
  return headerRef;
}
