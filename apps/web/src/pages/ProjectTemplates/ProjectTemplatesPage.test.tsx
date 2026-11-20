import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ProjectCreatePayload } from "@/api/projects";
import { projectTemplatesApi } from "@/api/projectTemplates";
import type { ProjectTemplateOut } from "@/api/projectTemplates";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { ToastRack } from "@/components/ui/Toast";
import { server } from "@/mocks/server";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ProjectTemplatesPage } from "./ProjectTemplatesPage";

function makeTemplate(
  overrides: Pick<ProjectTemplateOut, "id" | "name"> & Partial<ProjectTemplateOut>,
): ProjectTemplateOut {
  return {
    display_id: "PT-1",
    description: null,
    type_label: "图像-检测",
    type_key: "image-det",
    classes: ["car", "person"],
    classes_config: {},
    attribute_schema: { fields: [] },
    label_config: {},
    ai_enabled: false,
    sampling: "sequence",
    maximum_annotations: 1,
    show_overlap_first: false,
    iou_dedup_threshold: 0.7,
    box_threshold: 0.35,
    text_threshold: 0.25,
    rendering_config: {},
    annotation_guide: null,
    scope: "private",
    organization_id: null,
    created_by: "user-1",
    created_by_name: "creator",
    source_project_id: null,
    usage_count: 0,
    created_at: "2026-05-18T00:00:00Z",
    updated_at: "2026-05-18T00:00:00Z",
    ...overrides,
  };
}

const own = makeTemplate({ id: "port", name: "港口私有", created_by: "owner" });
const shared = makeTemplate({
  id: "buoy",
  name: "海域组织",
  scope: "organization",
  organization_id: "coast",
  classes: ["浮标"],
  classes_config: { 浮标: { color: "#123456", alias: "buoy", order: 0 } },
  annotation_guide: "检查可见水线",
  usage_count: 7,
  created_by: "colleague",
});
const published = makeTemplate({ id: "road", name: "公路公共", scope: "public" });
const listRoute = "*/api/v1/project-templates";

function serveList(rows: ProjectTemplateOut[]) {
  const queries: URLSearchParams[] = [];
  server.use(
    http.get(listRoute, ({ request }) => {
      const params = new URL(request.url).searchParams;
      queries.push(params);
      return HttpResponse.json(
        rows.filter(
          (t) =>
            (!params.has("scope") || t.scope === params.get("scope")) &&
            t.name.includes(params.get("search") ?? ""),
        ),
      );
    }),
  );
  return queries;
}

function LocationProbe() {
  const { search } = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location-search">{search}</output>
      <button onClick={() => navigate(-1)}>后退</button>
    </>
  );
}

function open(...entries: string[]) {
  renderWithProviders(
    <>
      <ProjectTemplatesPage />
      <DecisionDialogHost />
      <ToastRack />
    </>,
    {
      initialEntries: entries,
      children: <LocationProbe />,
    },
  );
}

const card = (id: string) => within(screen.getByTestId(`template-card-${id}`));

beforeEach(() => {
  resetUnexpectedApiRequests();
  seedAuthUser(createTestUser({ id: "owner", role: "project_admin" }));
  server.use(http.get("*/api/v1/projects", () => HttpResponse.json([])));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  resetAuthUser();
  expectNoUnexpectedApiRequests();
});

it("加载中的私有列表不显示空态，请求完成才提示新建", async () => {
  serveList([]);
  open("/project-templates");
  expect(screen.getByText("加载中…")).toBeVisible();
  expect(screen.queryByText(/暂无模板/)).not.toBeInTheDocument();
  expect(await screen.findByText(/暂无模板.*新建模板/)).toBeVisible();
  expect(screen.queryByText("加载中…")).not.toBeInTheDocument();
});

it("克隆可见的组织模板：只有自己拥有的卡片能编辑或删除", async () => {
  const rows = [own, shared, published];
  serveList(rows);
  const cloned: string[] = [];
  server.use(
    http.post(`${listRoute}/:id/duplicate`, ({ params }) => {
      const source = rows.find((r) => r.id === params.id)!;
      cloned.push(source.id);
      const copy = {
        ...source,
        id: "copy",
        name: `${source.name} (副本)`,
        scope: "private" as const,
        created_by: "owner",
      };
      rows.push(copy);
      return HttpResponse.json(copy, { status: 201 });
    }),
  );
  open("/project-templates?scope=all");
  await screen.findByText(own.name);
  expect(card(own.id).getByText("私有")).toBeVisible();
  expect(card(published.id).getByText("公共")).toBeVisible();
  expect(card(shared.id).getByText("组织")).toBeVisible();
  expect(card(shared.id).getByText("含指引")).toBeVisible();
  expect(card(shared.id).getByText("使用 7 次")).toBeVisible();
  expect(card(shared.id).getByText("1 个类别")).toBeVisible();
  for (const action of ["编辑", "删除"]) {
    expect(card(own.id).getByRole("button", { name: action })).toBeEnabled();
    expect(card(shared.id).queryByRole("button", { name: action })).not.toBeInTheDocument();
  }
  fireEvent.click(card(shared.id).getByRole("button", { name: "克隆" }));
  await screen.findByTestId("template-card-copy");
  expect(cloned).toEqual([shared.id]);
  expect(card("copy").getByText("海域组织 (副本)")).toBeVisible();
  expect(card("copy").getByText("私有")).toBeVisible();
  expect(card("copy").getByRole("button", { name: "编辑" })).toBeEnabled();
});

it("超级管理员删除他人的第二张模板：取消不删除，确认后只移除目标", async () => {
  seedAuthUser(createTestUser({ id: "super", role: "super_admin" }));
  const rows = [own, shared];
  serveList(rows);
  const deleted: string[] = [];
  server.use(
    http.delete(`${listRoute}/:id`, ({ params }) => {
      const index = rows.findIndex((r) => r.id === params.id);
      if (index < 0) return new HttpResponse(null, { status: 404 });
      deleted.push(rows.splice(index, 1)[0].id);
      return new HttpResponse(null, { status: 204 });
    }),
  );
  open("/project-templates?scope=all");
  await screen.findByText(shared.name);
  const remove = card(shared.id).getByRole("button", { name: "删除" });
  fireEvent.click(remove);
  const dialog = within(await screen.findByRole("alertdialog"));
  expect(dialog.getByText(/海域组织/)).toBeVisible();
  expect(dialog.getByText("已使用 7 次")).toBeVisible();
  expect(deleted).toEqual([]);
  fireEvent.click(dialog.getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  expect(deleted).toEqual([]);
  fireEvent.click(remove);
  fireEvent.click(
    within(await screen.findByRole("alertdialog")).getByRole("button", { name: "删除" }),
  );
  await waitFor(() => expect(screen.queryByText(shared.name)).not.toBeInTheDocument());
  expect(deleted).toEqual([shared.id]);
  expect(card(own.id).getByText(own.name)).toBeVisible();
});

it("应用第二张模板从其详情预填，显式创建时携带模板身份，成功后进入数据集步骤", async () => {
  const rows = [own, shared];
  serveList(rows);
  const details: string[] = [];
  const writes: ProjectCreatePayload[] = [];
  server.use(
    http.get(`${listRoute}/:id`, ({ params }) => {
      const template = rows.find((t) => t.id === params.id)!;
      details.push(template.id);
      return HttpResponse.json(template);
    }),
    http.post("*/api/v1/projects", async ({ request }) => {
      const body = (await request.json()) as ProjectCreatePayload;
      writes.push(body);
      return HttpResponse.json(
        { ...body, id: "new-project", display_id: "PROJ-42" },
        { status: 201 },
      );
    }),
    http.get("*/api/v1/datasets", () => HttpResponse.json({ items: [], total: 0 })),
  );
  open("/project-templates?scope=all");
  await screen.findByText(shared.name);
  fireEvent.click(card(shared.id).getByRole("button", { name: "应用" }));
  await screen.findByText(/已用模板字段预填表单/);
  expect(details).toEqual([shared.id]);
  fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
    target: { value: "  水线核查  " },
  });
  const next = () => fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
  next();
  const alias = screen.getByDisplayValue("buoy");
  fireEvent.change(alias, { target: { value: "harbor buoy" } });
  next();
  next();
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));
  expect(await screen.findByText("项目创建成功")).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({
    name: "水线核查",
    template_id: shared.id,
    source_project_id: null,
    classes: ["浮标"],
    tool_bindings: {
      bbox: { classes: [{ name: "浮标", color: "#123456", alias: "harbor buoy", order: 0 }] },
    },
  });
  expect(writes[0]).not.toHaveProperty("copy_annotation_guide");
  expect(screen.getByText(/选择要关联到本项目的数据集/)).toBeVisible();
});

it("地址栏恢复与切换范围按实际查询取行，后退不混用新范围和旧搜索词", async () => {
  const queries = serveList([own, shared, published]);
  open("/project-templates?q=港口", "/project-templates?scope=all&q=海域&keep=yes");
  expect(await screen.findByText(shared.name)).toBeVisible();
  expect(screen.getByPlaceholderText("搜索模板名称…")).toHaveValue("海域");
  expect(Object.fromEntries(queries[queries.length - 1])).toEqual({ search: "海域" });
  fireEvent.click(screen.getByRole("button", { name: "组织模板" }));
  await waitFor(() =>
    expect(Object.fromEntries(queries[queries.length - 1])).toEqual({
      scope: "organization",
      search: "海域",
    }),
  );
  expect(screen.getByTestId("location-search")).toHaveTextContent("keep=yes");
  fireEvent.click(screen.getByRole("button", { name: "后退" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true"),
  );
  fireEvent.click(screen.getByRole("button", { name: "后退" }));
  expect(await screen.findByText(own.name)).toBeVisible();
  expect(screen.getByPlaceholderText("搜索模板名称…")).toHaveValue("港口");
  expect(queries.filter((q) => q.get("scope") === "private").map((q) => q.get("search"))).toEqual([
    "港口",
  ]);
});

it("输入即写入地址栏，但防抖到期前不按新名称发请求", async () => {
  const queries = serveList([own]);
  const list = vi.spyOn(projectTemplatesApi, "list");
  open("/project-templates");
  await screen.findByText(own.name);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.change(screen.getByPlaceholderText("搜索模板名称…"), { target: { value: "没有匹配" } });
  expect(
    new URLSearchParams(screen.getByTestId("location-search").textContent ?? "").get("q"),
  ).toBe("没有匹配");
  act(() => {
    vi.advanceTimersByTime(249);
  });
  expect(list.mock.calls.map(([params]) => params?.search)).toEqual([undefined]);
  act(() => {
    vi.advanceTimersByTime(1);
  });
  expect(list.mock.calls.map(([params]) => params?.search)).toEqual([undefined, "没有匹配"]);
  vi.useRealTimers();
  expect(await screen.findByText(/暂无模板/)).toBeVisible();
  expect(queries.map((q) => q.get("search"))).toEqual([null, "没有匹配"]);
});
