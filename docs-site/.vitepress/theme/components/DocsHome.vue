<script setup lang="ts">
import { ref } from "vue";
import { withBase } from "vitepress";
import DataAtlasHero from "./home/DataAtlasHero.vue";
import ProductProof from "./home/ProductProof.vue";
import DocumentationPaths from "./home/DocumentationPaths.vue";
import DataProductionLoop from "./home/DataProductionLoop.vue";
import { useHomeMotion } from "./home/useHomeMotion";

const rootRef = ref<HTMLElement | null>(null);
useHomeMotion(rootRef);

// 能力 marquee：不显示易过期数字，只列多模态能力。
const capabilities = [
  "IMAGE ANNOTATION",
  "VIDEO TRACKING",
  "3D POINT CLOUD",
  "AI PRE-LABELING",
  "QUALITY REVIEW",
  "PRIVATE DEPLOYMENT",
];

// Final CTA：三个下一步，链接数据复用全站内容路由（不复制维护 GitHub/更新日志 URL）。
const finalLinks = [
  { label: "第一次使用平台", cta: "START ↗", href: withBase("/user-guide/getting-started") },
  { label: "部署到自己的基础设施", cta: "DEPLOY ↗", href: withBase("/ops/") },
  { label: "接入模型与 API", cta: "INTEGRATE ↗", href: withBase("/api/") },
];
</script>

<template>
  <div ref="rootRef" class="docs-home">
    <div class="grain" aria-hidden="true"></div>
    <div class="home-progress" aria-hidden="true"><i></i></div>

    <div class="hero-screen">
      <DataAtlasHero />

      <div class="marquee" aria-hidden="true">
        <div class="marquee-track">
          <span v-for="n in 2" :key="n">
            <template v-for="cap in capabilities" :key="cap + n">{{ cap }} <b>✦</b> </template>
          </span>
        </div>
      </div>
    </div>

    <ProductProof />
    <DocumentationPaths />
    <DataProductionLoop />

    <section class="final">
      <div class="final-top reveal">
        <h2>BUILD<br />BETTER<br />DATA.</h2>
        <div class="final-actions">
          <a v-for="l in finalLinks" :key="l.cta" class="final-link" :href="l.href">
            <span>{{ l.label }}</span
            ><b>{{ l.cta }}</b>
          </a>
        </div>
      </div>
      <div class="final-word" aria-hidden="true">ANNOTATE</div>
    </section>
  </div>
</template>
