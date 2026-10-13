import { http, HttpResponse } from "msw";

import pixelAvatarManifest from "../../public/avatars/pixel/manifest.json";

/**
 * MSW 默认 handlers。
 *
 * 原则：
 *   - 此处只放"全局通用"的最小 mock（auth/me、health、project list 空态等）
 *   - 单个测试如果要覆盖某个 endpoint，用 `server.use(http.get(..., ...))` 临时注入
 *   - mock 数据形态尽量参考 src/api/generated/types.gen.ts，保证类型一致
 *
 * 调试：在测试里加 `server.events.on("request:start", ({ request }) => console.log(request.url))`
 */

const API = "*/api/v1";

export const handlers = [
  http.get("*/health", () => HttpResponse.json({ status: "ok" })),

  // 内置像素头像目录：直接回放构建期生成的真实 manifest（public/avatars/pixel），
  // 避免单测里出现「目录加载失败」的假失败。
  http.get("*/avatars/pixel/manifest.json", () => HttpResponse.json(pixelAvatarManifest)),

  http.post(`${API}/auth/login`, async () =>
    HttpResponse.json({
      access_token: "mock-token",
      refresh_token: "mock-refresh",
      token_type: "bearer",
    }),
  ),

  http.get(`${API}/auth/me`, () =>
    HttpResponse.json({
      id: 1,
      email: "tester@example.com",
      name: "Tester",
      role: "annotator",
      status: "active",
    }),
  ),

  http.get(`${API}/projects`, () => HttpResponse.json({ items: [], total: 0, page: 1, size: 20 })),

  http.get(`${API}/projects/stats`, () =>
    HttpResponse.json({
      total_projects: 0,
      total_tasks: 0,
      completed_tasks: 0,
      pending_tasks: 0,
    }),
  ),
];
