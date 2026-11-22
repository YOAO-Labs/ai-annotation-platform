<script setup lang="ts">
import { computed } from "vue";
import { withBase } from "vitepress";

const props = defineProps<{
  src: string;
  alt: string;
  caption?: string;
}>();

function publicUrl(src: string): string {
  if (/^(?:[a-z]+:|\/\/)/i.test(src)) return src;
  return withBase(src.startsWith("/") ? src : `/${src}`);
}

const resolvedSrc = computed(() => publicUrl(props.src));
const downloadName = computed(() => {
  const clean = props.src.split(/[?#]/, 1)[0];
  return clean.split("/").at(-1) || "diagram.svg";
});
</script>

<template>
  <figure class="excalidraw-diagram">
    <div class="excalidraw-diagram__frame">
      <img class="excalidraw-diagram__image" :src="resolvedSrc" :alt="alt" />
    </div>
    <figcaption>
      <span v-if="caption">{{ caption }}</span>
      <a :href="resolvedSrc" :download="downloadName" :aria-label="`下载可编辑 SVG：${alt}`">
        下载可编辑 SVG
      </a>
    </figcaption>
  </figure>
</template>

<style scoped>
.excalidraw-diagram {
  margin: 1.75rem 0;
}

.excalidraw-diagram__frame {
  overflow: hidden;
  padding: 0.75rem;
  border: 1px solid var(--vp-c-border);
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.excalidraw-diagram__frame img {
  display: block;
  width: 100%;
  max-width: 100%;
  border: 0;
  border-radius: 0;
}

figcaption {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.4rem 0.75rem;
  margin-top: 0.55rem;
  color: var(--vp-c-text-2);
  font-size: 0.8rem;
  line-height: 1.5;
}

figcaption a {
  margin-left: auto;
  color: var(--vp-c-brand-1);
  font-weight: 600;
  text-decoration: none;
}

figcaption a:hover {
  text-decoration: underline;
}
</style>
