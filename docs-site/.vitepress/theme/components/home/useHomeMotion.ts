import { onMounted, onUnmounted, type Ref } from "vue";

export function useHomeMotion(rootRef: Ref<HTMLElement | null>): void {
  let io: IntersectionObserver | null = null;
  let onScroll: (() => void) | null = null;
  let onPointer: ((e: PointerEvent) => void) | null = null;
  const magneticCleanups: Array<() => void> = [];
  let rafScroll = 0;
  let rafPointer = 0;

  onMounted(() => {
    const root = rootRef.value;
    if (!root) return;

    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const finePointer = window.matchMedia("(pointer: fine)").matches;

    if (reduce) return;

    root.classList.add("home-motion-ready");

    const reveals = Array.from(root.querySelectorAll<HTMLElement>(".reveal"));
    io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io?.unobserve(e.target);
          }
        });
      },
      { threshold: 0.14 },
    );
    reveals.forEach((el) => io!.observe(el));

    const progress = root.querySelector<HTMLElement>(".home-progress i");
    const figure = root.querySelector<HTMLElement>(".hero-figure");
    const screen = root.querySelector<HTMLElement>(".proof-screen");
    const homePage = document.querySelector<HTMLElement>(".docs-home-page");

    const applyScroll = () => {
      rafScroll = 0;
      const max = document.documentElement.scrollHeight - window.innerHeight;
      const p = max > 0 ? window.scrollY / max : 0;
      if (progress) progress.style.width = `${p * 100}%`;
      if (homePage) {
        homePage.classList.toggle(
          "home-nav-transparent",
          window.scrollY < window.innerHeight * 0.6,
        );
      }
      if (finePointer) {
        if (figure) {
          const y = Math.min(window.scrollY, window.innerHeight);
          figure.style.transform = `rotate(${-2.2 + y * 0.0015}deg) translateY(${y * 0.04}px)`;
        }
        if (screen) {
          const r = screen.getBoundingClientRect();
          const t = Math.max(
            -1,
            Math.min(1, (r.top - window.innerHeight * 0.5) / window.innerHeight),
          );
          screen.style.transform = `rotateX(${3 + t * 2}deg) scale(${0.96 + Math.max(0, -t) * 0.035})`;
        }
      }
    };
    onScroll = () => {
      if (!rafScroll) rafScroll = requestAnimationFrame(applyScroll);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    applyScroll();

    if (finePointer) {
      const atlas = root.querySelector<HTMLElement>(".hero-atlas");
      let px = 0;
      let py = 0;
      const applyPointer = () => {
        rafPointer = 0;
        if (atlas) atlas.style.transform = `translate(${px}px, ${py}px)`;
      };
      onPointer = (e: PointerEvent) => {
        px = (e.clientX / window.innerWidth - 0.5) * 8;
        py = (e.clientY / window.innerHeight - 0.5) * 8;
        if (!rafPointer) rafPointer = requestAnimationFrame(applyPointer);
      };
      window.addEventListener("pointermove", onPointer, { passive: true });

      root.querySelectorAll<HTMLElement>(".magnetic").forEach((el) => {
        const move = (e: PointerEvent) => {
          const r = el.getBoundingClientRect();
          el.style.transform = `translate(${(e.clientX - r.left - r.width / 2) * 0.09}px, ${(e.clientY - r.top - r.height / 2) * 0.13}px)`;
        };
        const leave = () => {
          el.style.transform = "";
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerleave", leave);
        magneticCleanups.push(() => {
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerleave", leave);
        });
      });
    }
  });

  onUnmounted(() => {
    io?.disconnect();
    io = null;
    if (onScroll) window.removeEventListener("scroll", onScroll);
    if (onPointer) window.removeEventListener("pointermove", onPointer);
    if (rafScroll) cancelAnimationFrame(rafScroll);
    if (rafPointer) cancelAnimationFrame(rafPointer);
    magneticCleanups.forEach((fn) => fn());
    magneticCleanups.length = 0;
    document.querySelector(".docs-home-page")?.classList.remove("home-nav-transparent");
  });
}
