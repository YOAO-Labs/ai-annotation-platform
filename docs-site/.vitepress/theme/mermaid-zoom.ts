import type svgPanZoomFn from "svg-pan-zoom";

const ZOOM_VIEWPORT_RATIO = 0.7;
const MIN_BOX_HEIGHT = 240;

export function setupMermaidZoom(): void {
  if (typeof window === "undefined") return;

  let svgPanZoom: typeof svgPanZoomFn | null = null;

  const initOne = async (svg: SVGSVGElement) => {
    if (svg.dataset.zoomReady) return;
    const vb = svg.viewBox?.baseVal;
    if (!vb || vb.width === 0) return;
    svg.dataset.zoomReady = "1";

    if (!svgPanZoom) {
      svgPanZoom = (await import("svg-pan-zoom")).default;
    }

    const natural = svg.getBoundingClientRect().height || 320;
    const boxH = Math.max(
      MIN_BOX_HEIGHT,
      Math.min(natural, Math.round(window.innerHeight * ZOOM_VIEWPORT_RATIO)),
    );

    const wrapper = document.createElement("div");
    wrapper.className = "mermaid-zoom";
    wrapper.style.height = `${boxH}px`;
    svg.parentNode?.insertBefore(wrapper, svg);
    wrapper.appendChild(svg);

    svg.style.maxWidth = "none";
    svg.style.width = "";
    svg.style.height = "";
    const applyPxSize = () => {
      svg.setAttribute("width", String(wrapper.clientWidth));
      svg.setAttribute("height", String(wrapper.clientHeight));
    };
    applyPxSize();

    const instance = svgPanZoom!(svg, {
      zoomEnabled: true,
      controlIconsEnabled: false,
      fit: true,
      center: true,
      minZoom: 0.4,
      maxZoom: 15,
      zoomScaleSensitivity: 0.3,
      mouseWheelZoomEnabled: true,
      dblClickZoomEnabled: false,
    });

    const mkBtn = (label: string, title: string, onClick: () => void) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mermaid-zoom-btn";
      b.textContent = label;
      b.title = title;
      b.addEventListener("click", (e) => {
        e.preventDefault();
        onClick();
      });
      return b;
    };
    const bar = document.createElement("div");
    bar.className = "mermaid-zoom-ctrl";
    bar.append(
      mkBtn("+", "放大", () => instance.zoomIn()),
      mkBtn("−", "缩小", () => instance.zoomOut()),
      mkBtn("⟲", "复位", () => {
        instance.reset();
        instance.fit();
        instance.center();
      }),
    );
    wrapper.appendChild(bar);

    new ResizeObserver(() => {
      applyPxSize();
      instance.resize();
      instance.fit();
      instance.center();
    }).observe(wrapper);
  };

  const scan = () => {
    document
      .querySelectorAll<SVGSVGElement>('svg[id^="mermaid-"]')
      .forEach((svg) => void initOne(svg));
  };

  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      scan();
    });
  };
  const observer = new MutationObserver(schedule);

  const start = () => {
    observer.observe(document.body, { childList: true, subtree: true });
    scan();
    [300, 1000, 2500].forEach((d) => setTimeout(scan, d));
  };

  if (document.body) start();
  else window.addEventListener("DOMContentLoaded", start);
}
