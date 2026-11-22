<script setup lang="ts">
import { ref } from "vue";
import { withBase } from "vitepress";

const src = withBase("/api-reference.html");
const loaded = ref(false);
</script>

<template>
  <div class="api-ref-frame">
    <div class="api-ref-bar">
      <span class="api-ref-label">OpenAPI Reference</span>
      <a class="api-ref-fullscreen" :href="src" target="_blank" rel="noreferrer"> 全屏打开 ↗ </a>
    </div>
    <div class="api-ref-viewport">
      <div v-if="!loaded" class="api-ref-loading" aria-hidden="true">加载 API Reference…</div>
      <iframe :src="src" title="API Reference" loading="lazy" @load="loaded = true"></iframe>
    </div>
  </div>
</template>

<style scoped>
.api-ref-frame {
  margin: 24px 0 8px;
  border: 1px solid var(--vp-c-border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--vp-c-bg-soft);
}
.api-ref-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 14px;
  border-bottom: 1px solid var(--vp-c-border);
  background: var(--vp-c-bg-alt);
}
.api-ref-label {
  font-family: var(--docs-font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--vp-c-text-2);
}
.api-ref-fullscreen {
  font-size: 12px;
  font-weight: 500;
  color: var(--vp-c-brand-1) !important;
  text-decoration: none !important;
}
.api-ref-viewport {
  position: relative;
  height: min(80vh, 900px);
  background: var(--vp-c-bg);
}
.api-ref-loading {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 13px;
  color: var(--vp-c-text-3);
}
.api-ref-viewport iframe {
  position: relative;
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
}
@media (max-width: 640px) {
  .api-ref-viewport {
    height: 70vh;
  }
}
</style>
