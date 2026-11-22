import { http, HttpResponse } from "msw";

import pixelAvatarManifest from "../../public/avatars/pixel/manifest.json";

const API = "*/api/v1";

export const handlers = [
  http.get("*/health", () => HttpResponse.json({ status: "ok" })),

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
