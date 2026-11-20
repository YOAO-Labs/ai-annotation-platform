import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, expect, it } from "vitest";
import { server } from "@/mocks/server";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ModelMarketPage } from "./ModelMarketPage";

afterEach(() => {
  cleanup();
  resetAuthUser();
  localStorage.removeItem("token");
});

it("项目管理员仅用目录和协议的只读接口就能浏览模型，不读取超管观测", async () => {
  seedAuthUser(createTestUser({ role: "project_admin" }));
  const unexpected: string[] = [];
  server.use(
    http.get("*/api/v1/model-catalog", () =>
      HttpResponse.json({
        items: [],
        total: 0,
        page: 1,
        page_size: 200,
        projects: [{ id: "owned", name: "仅我管理的项目" }],
        task_counts: {},
      }),
    ),
    http.get("*/api/v1/ml-capabilities/protocol", () =>
      HttpResponse.json({
        version: "2",
        infras: [],
        prompts: [],
        inputs: [],
        modalities: [],
        geometries: [],
        tasks: [
          {
            id: "detect",
            label: "公开检测协议",
            summary: "公共协议",
            default_geometry: [],
            default_modalities: [],
            typical_models: [],
            suggested_backends: [],
            protocol_notes: "",
          },
        ],
      }),
    ),
    http.all("*/api/v1/*", ({ request }) => {
      unexpected.push(`${request.method} ${new URL(request.url).pathname}`);
      return HttpResponse.json({ detail: "outside project catalog access" }, { status: 403 });
    }),
  );
  const user = userEvent.setup();
  renderWithProviders(<ModelMarketPage />, { initialEntries: ["/model-market?tab=runtime"] });
  expect(await screen.findByRole("option", { name: "仅我管理的项目" })).toBeInTheDocument();
  expect(await screen.findByText(/没有匹配的模型/)).toBeVisible();
  expect(screen.queryByRole("tab", { name: /运行时观测/ })).not.toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /注册管理/ })).toBeVisible();
  await user.click(screen.getByRole("tab", { name: /协议能力/ }));
  expect(await screen.findByRole("heading", { name: "公开检测协议 detect" })).toBeVisible();
  expect(screen.getByRole("link", { name: "查看模型（0）" })).toHaveAttribute(
    "href",
    "/model-market?model_task=detect",
  );
  expect(unexpected).toEqual([]);
});
