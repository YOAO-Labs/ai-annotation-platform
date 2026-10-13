<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { withBase } from "vitepress";
import videoWorkspaceUrl from "../../assets/home/hero/video-track.webp";
import pointCloudWorkspaceUrl from "../../assets/home/hero/pointcloud.webp";
import dataManagerUrl from "../../assets/home/hero/data-manager.webp";
import reviewWorkspaceUrl from "../../assets/home/hero/review.webp";

const getStartedHref = withBase("/user-guide/getting-started");
const heroWorkspaceUrl = withBase("/home/ai-assisted-annotation-poster.webp");

type HeroSlide = {
  no: string;
  kicker: string;
  title: string;
  src: string;
  href: string;
  alt: string;
};

const heroSlides: HeroSlide[] = [
  {
    no: "01",
    kicker: "IMAGE / SAM3",
    title: "AI 交互标注",
    src: heroWorkspaceUrl,
    href: withBase("/user-guide/workbench/sam-tool"),
    alt: "真实道路图中使用 SAM3 标注白色车辆并进入人工类别确认",
  },
  {
    no: "02",
    kicker: "VIDEO / TRACKS",
    title: "视频轨迹标注",
    src: videoWorkspaceUrl,
    href: withBase("/user-guide/workbench/video-track"),
    alt: "城市交通视频在标注工作台中显示时间轴与轨迹工具",
  },
  {
    no: "03",
    kicker: "3D / POINT CLOUD",
    title: "点云标注工作台",
    src: pointCloudWorkspaceUrl,
    href: withBase("/user-guide/workbench/pointcloud-view"),
    alt: "nuScenes 道路场景点云与相机视图在三维标注工作台中渲染",
  },
  {
    no: "04",
    kicker: "DATA / MANAGER",
    title: "任务与数据视图",
    src: dataManagerUrl,
    href: withBase("/user-guide/projects/data-manager"),
    alt: "Data Manager 中的任务统计、视图与数据列表",
  },
  {
    no: "05",
    kicker: "QUALITY / REVIEW",
    title: "质检审阅工作台",
    src: reviewWorkspaceUrl,
    href: withBase("/user-guide/review/"),
    alt: "道路车辆标注在质检工作台中等待审阅",
  },
];

const AUTO_DELAY_MS = 5_200;
const DRAW_DURATION_MS = 560;
const deckRef = ref<HTMLElement | null>(null);
const deckOrder = ref(heroSlides.map((_, index) => index));
const drawnSlideIndex = ref<number | null>(null);
const deckAnimating = ref(false);
const deckHovered = ref(false);
const deckFocused = ref(false);
const motionAllowed = ref(false);
const mediaWindow = computed(() => new Set(deckOrder.value.slice(0, 2)));
const deckPaused = computed(() => !motionAllowed.value || deckHovered.value || deckFocused.value);

let autoplayTimer: number | undefined;
let drawTimer: number | undefined;
let reducedMotionQuery: MediaQueryList | undefined;
const slidePreloads = new Map<number, Promise<void>>();

function clearAutoplay(): void {
  if (autoplayTimer !== undefined) window.clearTimeout(autoplayTimer);
  autoplayTimer = undefined;
}

function scheduleAutoplay(): void {
  clearAutoplay();
  if (deckPaused.value) return;
  autoplayTimer = window.setTimeout(() => {
    void cycleNext(false);
  }, AUTO_DELAY_MS);
}

function finishManualMove(): void {
  if (drawTimer !== undefined) window.clearTimeout(drawTimer);
  drawTimer = window.setTimeout(() => {
    deckAnimating.value = false;
    scheduleAutoplay();
  }, DRAW_DURATION_MS);
}

function preloadSlide(index: number): Promise<void> {
  const cached = slidePreloads.get(index);
  if (cached) return cached;

  const promise = new Promise<void>((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => {
      void image
        .decode()
        .catch(() => undefined)
        .then(() => resolve());
    };
    image.onerror = () => reject(new Error(`Failed to preload hero slide ${index + 1}`));
    image.src = heroSlides[index]?.src ?? "";
  }).catch((error) => {
    slidePreloads.delete(index);
    throw error;
  });

  slidePreloads.set(index, promise);
  return promise;
}

async function cycleNext(resetAutoplay = true): Promise<void> {
  if (deckAnimating.value) return;
  if (resetAutoplay) clearAutoplay();

  const target = deckOrder.value[1];
  if (target === undefined) return;
  deckAnimating.value = true;
  try {
    await preloadSlide(target);
  } catch {
    deckAnimating.value = false;
    scheduleAutoplay();
    return;
  }

  if (!resetAutoplay && deckPaused.value) {
    deckAnimating.value = false;
    return;
  }

  if (!motionAllowed.value) {
    const first = deckOrder.value[0];
    if (first === undefined) {
      deckAnimating.value = false;
      return;
    }
    deckOrder.value = [...deckOrder.value.slice(1), first];
    deckAnimating.value = false;
    scheduleAutoplay();
    return;
  }

  const first = deckOrder.value[0];
  if (first === undefined) {
    deckAnimating.value = false;
    return;
  }
  drawnSlideIndex.value = first;
  if (drawTimer !== undefined) window.clearTimeout(drawTimer);
  drawTimer = window.setTimeout(() => {
    deckOrder.value = [...deckOrder.value.slice(1), first];
    drawnSlideIndex.value = null;
    deckAnimating.value = false;
    scheduleAutoplay();
  }, DRAW_DURATION_MS);
}

async function showPrevious(): Promise<void> {
  if (deckAnimating.value) return;
  clearAutoplay();
  const nextOrder = [...deckOrder.value];
  const last = nextOrder.pop();
  if (last === undefined) return;
  deckAnimating.value = true;
  try {
    await preloadSlide(last);
  } catch {
    deckAnimating.value = false;
    scheduleAutoplay();
    return;
  }
  deckOrder.value = [last, ...nextOrder];
  finishManualMove();
}

function cardPosition(index: number): number {
  return deckOrder.value.indexOf(index);
}

function syncAutoplay(): void {
  if (deckPaused.value) clearAutoplay();
  else scheduleAutoplay();
}

function onDeckMouseEnter(): void {
  deckHovered.value = true;
  syncAutoplay();
}

function onDeckMouseLeave(): void {
  deckHovered.value = false;
  syncAutoplay();
}

function onDeckFocusIn(): void {
  deckFocused.value = true;
  syncAutoplay();
}

function onDeckFocusOut(): void {
  void nextTick(() => {
    deckFocused.value = deckRef.value?.contains(document.activeElement) ?? false;
    syncAutoplay();
  });
}

function onReducedMotionChange(event: MediaQueryListEvent): void {
  motionAllowed.value = !event.matches;
  syncAutoplay();
}

onMounted(() => {
  reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  motionAllowed.value = !reducedMotionQuery.matches;
  reducedMotionQuery.addEventListener("change", onReducedMotionChange);
  scheduleAutoplay();
});

onBeforeUnmount(() => {
  clearAutoplay();
  if (drawTimer !== undefined) window.clearTimeout(drawTimer);
  reducedMotionQuery?.removeEventListener("change", onReducedMotionChange);
});

/** 复用现有 VitePress local search：优先点击导航栏搜索按钮，回退到 ⌘K 快捷键。 */
function openSearch(): void {
  const btn = document.querySelector<HTMLElement>(
    ".VPNavBarSearch button, .VPNavBarSearchButton, button.DocSearch-Button",
  );
  if (btn) {
    btn.click();
    return;
  }
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true, ctrlKey: true }));
}
</script>

<template>
  <header class="hero">
    <div class="hero-grid" aria-hidden="true"></div>

    <div class="hero-copy">
      <div class="overline">Open source · Human in the loop</div>
      <h1>THE DATA<br />THAT TRAINS<br /><em>INTELLIGENCE</em></h1>
      <p class="hero-desc">
        把图像、视频和点云，从复杂原始数据转化为可审核、可追踪、可复用的训练资产。
      </p>
      <div class="hero-actions">
        <a class="hero-btn magnetic" :href="getStartedHref">
          打开使用文档 <span aria-hidden="true">↗</span>
        </a>
        <button class="search-line" type="button" @click="openSearch">
          <span>SEARCH THE KNOWLEDGE BASE</span><b>⌘ K</b>
        </button>
      </div>
    </div>

    <div class="hero-art">
      <svg class="hero-atlas" viewBox="0 0 700 760" aria-hidden="true" focusable="false">
        <path class="orbit" d="M78 360C130 94 490 30 638 266S527 692 258 642 15 481 78 360Z" />
        <path class="orbit" d="M126 550C46 308 244 67 493 126S718 483 515 636 188 708 126 550Z" />
        <path class="orbit" d="M89 225C244 48 586 117 633 380S368 726 150 565-24 352 89 225Z" />
        <g class="ray">
          <path
            d="M350 380 74 38M350 380 128 17M350 380 188 7M350 380 247 0M350 380 309 2M350 380 371 0M350 380 438 8M350 380 502 21M350 380 561 48M350 380 617 89M350 380 662 142M350 380 688 206M350 380 700 278M350 380 697 350M350 380 690 426M350 380 670 497M350 380 635 559M350 380 589 616M350 380 530 663M350 380 464 701M350 380 393 728M350 380 321 739M350 380 250 728M350 380 181 705M350 380 119 666M350 380 68 613M350 380 29 550M350 380 8 480M350 380 0 407"
          />
        </g>
        <rect class="frame" x="118" y="188" width="145" height="116" />
        <rect class="frame" x="469" y="440" width="112" height="142" />
        <circle class="node" cx="118" cy="188" r="5" />
        <circle class="node" cx="580" cy="581" r="5" />
      </svg>

      <div class="art-label" aria-hidden="true">ANNOTATION ATLAS · LIVE SIGNALS</div>

      <div
        ref="deckRef"
        class="hero-figure hero-deck"
        role="region"
        aria-roledescription="carousel"
        aria-label="标注平台关键页面"
        @mouseenter="onDeckMouseEnter"
        @mouseleave="onDeckMouseLeave"
        @focusin="onDeckFocusIn"
        @focusout="onDeckFocusOut"
      >
        <div class="hero-deck-space" aria-hidden="true"></div>

        <a
          v-for="(slide, index) in heroSlides"
          :key="slide.no"
          class="hero-card"
          :class="[
            `hero-card-pos-${cardPosition(index)}`,
            { 'is-drawing': drawnSlideIndex === index },
          ]"
          :href="slide.href"
          :aria-hidden="cardPosition(index) !== 0"
          :tabindex="cardPosition(index) === 0 ? 0 : -1"
        >
          <span class="hero-card-head">
            <span>LIVE ROUTE / {{ slide.no }}</span>
            <small>{{ slide.kicker }}</small>
          </span>
          <span class="hero-card-media">
            <img
              v-if="mediaWindow.has(index)"
              :src="slide.src"
              :alt="slide.alt"
              loading="eager"
              decoding="async"
              :fetchpriority="cardPosition(index) === 0 ? 'high' : 'low'"
            />
          </span>
          <span class="hero-card-foot">
            <strong>{{ slide.title }}</strong>
            <small>OPEN GUIDE ↗</small>
          </span>
        </a>

        <div class="hero-deck-nav" aria-label="切换平台页面">
          <button
            class="hero-deck-prev"
            type="button"
            aria-label="上一个平台页面"
            @click="showPrevious()"
          >
            ←
          </button>
          <button
            class="hero-deck-next"
            type="button"
            aria-label="下一个平台页面"
            @click="cycleNext()"
          >
            →
          </button>
        </div>
      </div>
    </div>

    <div class="hero-index" aria-hidden="true">01 / PLATFORM OVERVIEW</div>
  </header>
</template>
