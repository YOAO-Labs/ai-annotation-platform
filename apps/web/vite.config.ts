/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "path";
import { cspNoncePlugin } from "./vite-plugins/csp-nonce";
import { releaseNotesPlugin } from "./vite-plugins/release-notes";

const apiTarget = process.env.API_PROXY_TARGET || "http://127.0.0.1:8000";
const wsTarget = apiTarget.replace(/^http/, "ws");
const minioTarget = process.env.MINIO_PROXY_TARGET || "http://localhost:9000";

const config: Parameters<typeof defineConfig>[0] = {
  plugins: [react(), tailwindcss(), cspNoncePlugin(), releaseNotesPlugin()],
  cacheDir: resolve(__dirname, "../../.vite/apps-web"),
  envDir: resolve(__dirname, "../../"),
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
    },
  },
  build: {
    rollupOptions: {
      output: {
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
  worker: {
    format: "es",
  },
  server: {
    port: process.env.PORT ? Number(process.env.PORT) : 3000,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/ws": {
        target: wsTarget,
        ws: true,
      },
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
    runner: resolve(__dirname, "src/test/apiRequestGuard.ts"),
    css: false,
    exclude: ["**/node_modules/**", "**/dist/**", "**/e2e/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html", "lcov"],
      reportsDirectory: "./coverage",
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
        "src/test/**",
        "**/*.test.{ts,tsx}",
        "**/*.spec.{ts,tsx}",
        "**/__tests__/**",
      ],
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
