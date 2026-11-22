<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { withBase } from "vitepress";

const props = withDefaults(
  defineProps<{
    src: string;
    poster?: string;
    alt: string;
    caption?: string;
    autoplay?: boolean;
    loop?: boolean;
  }>(),
  { autoplay: true, loop: true },
);

const container = ref<HTMLElement | null>(null);
const video = ref<HTMLVideoElement | null>(null);
const sourceMounted = ref(false);
const prefersReducedMotion = ref(false);
let visibilityObserver: IntersectionObserver | null = null;

const resolveSource = (source?: string) => {
  if (!source) return undefined;
  return /^(?:https?:|data:)/.test(source) ? source : withBase(source);
};
const videoSource = computed(() => resolveSource(props.src));
const posterSource = computed(() => resolveSource(props.poster));

async function enterViewport() {
  if (!sourceMounted.value) {
    sourceMounted.value = true;
    await nextTick();
    video.value?.load();
  }
  if (!props.autoplay || prefersReducedMotion.value) return;
  await video.value?.play().catch(() => {});
}

onMounted(() => {
  prefersReducedMotion.value = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!("IntersectionObserver" in window) || !container.value) {
    void enterViewport();
    return;
  }

  visibilityObserver = new IntersectionObserver(
    ([entry]) => {
      if (entry?.isIntersecting) {
        void enterViewport();
      } else {
        video.value?.pause();
      }
    },
    { threshold: 0.1 },
  );
  visibilityObserver.observe(container.value);
});

onBeforeUnmount(() => {
  visibilityObserver?.disconnect();
  video.value?.pause();
});
</script>

<template>
  <figure ref="container" class="docs-video">
    <video
      ref="video"
      :aria-label="alt"
      :data-source-mounted="sourceMounted"
      :poster="posterSource"
      :loop="loop"
      muted
      controls
      playsinline
      preload="metadata"
    >
      <source v-if="sourceMounted" :src="videoSource" type="video/mp4" />
      {{ alt }}
    </video>
    <figcaption v-if="caption">{{ caption }}</figcaption>
  </figure>
</template>

<style scoped>
.docs-video {
  margin: 1.75rem 0;
}

.docs-video video {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border: 1px solid var(--vp-c-border);
  border-radius: 8px;
  background: var(--vp-c-bg-soft);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.docs-video figcaption {
  margin-top: 0.55rem;
  color: var(--vp-c-text-2);
  font-size: 0.8rem;
  line-height: 1.5;
}
</style>
