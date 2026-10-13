/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "path";
import { cspNoncePlugin } from "./vite-plugins/csp-nonce";
import { releaseNotesPlugin } from "./vite-plugins/release-notes";

// Dev proxy target is configurable: parallel worktrees run their backend on
// different ports, overridden with API_PROXY_TARGET; defaults to 8000.
const apiTarget = process.env.API_PROXY_TARGET || "http://127.0.0.1:8000";
const wsTarget = apiTarget.replace(/^http/, "ws");
const minioTarget = process.env.MINIO_PROXY_TARGET || "http://127.0.0.1:9000";

// vitest 字段在 vite 6 的 UserConfig 类型里未直接合并，用类型断言放过。
// `/// <reference types="vitest" />` 已注入运行时 schema。
const config: Parameters<typeof defineConfig>[0] = {
  plugins: [react(), tailwindcss(), cspNoncePlugin(), releaseNotesPlugin()],
  // Worktrees may share node_modules, but Vite's optimizer cache is tied to one
  // running module graph. Keep it in the checkout so parallel dev servers cannot
  // overwrite each other's pre-bundled dependencies.
  cacheDir: resolve(__dirname, "../../.vite/apps-web"),
  // The repository-root `.env` is the shared front/back source of truth. Vite
  // would otherwise read `apps/web/.env` and drift from backend settings;
  // pointing at the root keeps VITE_* variables in sync.
  envDir: resolve(__dirname, "../../"),
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split vendor chunks so the main bundle stays below the size warning.
        manualChunks(id) {
          if (/\/node_modules\/(?:react|react-dom|scheduler)\//.test(id)) return "vendor-react";
          if (/\/node_modules\/(?:dockview|dockview-core|dockview-react)\//.test(id))
            return "vendor-dockview";
          if (/\/node_modules\/(?:konva|react-konva)\//.test(id)) return "vendor-konva";
          if (id.includes("/node_modules/react-markdown/")) return "vendor-markdown";
          if (id.includes("/node_modules/three/")) return "vendor-three";
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
  // Both production Workers are module Workers. ES output is also required for
  // the Raster Mask Worker's lazy WebGPU provider chunk.
  worker: {
    format: "es",
  },
  server: {
    port: process.env.PORT ? Number(process.env.PORT) : 3000,
    proxy: {
      // 用 127.0.0.1 强制 IPv4：CI runner 上 Node 把 localhost 解析成 ::1，但
      // 后端 uvicorn 只绑 IPv4，会触发 ECONNREFUSED ::1:8000。
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/ws": {
        target: wsTarget,
        ws: true,
      },
      // DEV 媒体走与页面同源的 /minio，远程浏览器只需能访问 Vite
      // 端口。转发前去掉前缀，保持 S3 签名的 canonical resource 不变。
      "/minio": {
        target: minioTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/minio/, ""),
      },
    },
  },
  // @ts-expect-error vite 6 typing 不暴露 test 字段；运行时由 vitest 解析
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    // e2e/** is executed by Playwright or `node --test`, not collected by vitest.
    // scripts/media-derivation.test.mjs is node:test style (run with
    // `node --test` by screenshots:docs-media:test) and would report "No test
    // suite found"; the remaining scripts/*.test.mjs files are vitest style and
    // must stay collected.
    exclude: ["**/node_modules/**", "**/dist/**", "**/e2e/**", "scripts/media-derivation.test.mjs"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html", "lcov"],
      reportsDirectory: "./coverage",
      // Coverage counts real product source only. Excluded: generated types,
      // static data, entry/CSS scaffolding, jsdom-incompatible canvas/WebGL
      // renderers, build-time scripts, test files and test infrastructure.
      //
      // The canvas/WebGL exclusions do not claim those surfaces are untested:
      // geometry keeps its low-level unit tests under stages/three-d/geometry/**
      // and real canvas/WebGL interaction stays with browser validation. They
      // only stay out of a jsdom coverage denominator that cannot exercise them.
      exclude: [
        "src/api/generated/**",
        "src/mocks/**",
        "src/types/**",
        "src/data/**",
        "src/main.tsx",
        "src/index.css",
        "src/vite-env.d.ts",
        "src/pages/Workbench/stage/tools/**",
        "src/pages/Workbench/stage/Stage.tsx",
        "src/pages/Workbench/stage/Minimap.tsx",
        "src/pages/Workbench/stage/Layers.tsx",
        "src/pages/Workbench/stages/three-d/*.{ts,tsx}",
        "src/utils/bugReportCapture.ts",
        "src/utils/uploadQueue.ts",
        "src/components/bugreport/**",
        "**/*.config.{ts,js}",
        "**/*.d.ts",
        "e2e/**",
        "dist/**",
        "scripts/**",
        // Test-only support code (render fixtures, MSW request guard, Konva
        // stand-in) is infrastructure, not product source.
        "src/test/**",
        "**/*.test.{ts,tsx}",
        "**/*.spec.{ts,tsx}",
        "**/__tests__/**",
      ],
      // Hard gate: below these thresholds vitest exits non-zero, and
      // codecov.yml keeps `frontend` informational=false, so coverage cannot
      // regress. Cover the new denominator or add tests; never widen exclusions
      // or lower a threshold to go green.
      thresholds: {
        lines: 45,
        statements: 45,
        functions: 45,
        branches: 70,
      },
    },
  },
};

export default defineConfig(config);
