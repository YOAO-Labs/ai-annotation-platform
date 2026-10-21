import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { modelLineageApi, lineageReturn, type LineageOut } from "@/api/modelLineage";
import { useAuthStore } from "@/stores/authStore";
import { ModelLineagePage, ModelLineagePanel } from "./ModelLineagePage";

vi.mock("@/api/modelLineage", async (original) => ({
  ...(await original<typeof import("@/api/modelLineage")>()),
  modelLineageApi: { get: vi.fn(), decisions: vi.fn(), append: vi.fn() },
}));
const node = {
  kind: "model",
  id: "candidate",
  name: "雨天车辆候选",
  created_at: "2026-10-15T08:00:00Z",
  facts: { frozen_attempt_id: "old-attempt" },
} as const;
const result: LineageOut = {
  node,
  inputs: [
    {
      relation: "exact_attempt",
      target: {
        kind: "attempt",
        id: "old-attempt",
        name: "执行尝试 1",
        created_at: node.created_at,
        facts: { run_id: "experiment" },
      },
    },
    { relation: "frozen_provenance", gap: "历史摘要缺失" },
  ],
  sections: ["evaluations", "predictions"],
  items: [],
};
const clients: QueryClient[] = [];
let navigate: ReturnType<typeof useNavigate>;
function Navigation() {
  navigate = useNavigate();
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}
function ModelPanelRoute() {
  const { id = "", modelId = "" } = useParams();
  const location = useLocation();
  const view = new URLSearchParams(location.search).get("model_view");
  return (
    <ModelLineagePanel
      projectId={id}
      modelId={modelId}
      view={view === "notes" ? "notes" : "records"}
    />
  );
}
function mount(
  entry = "/projects/project/learning/lineage/model/candidate?returnTo=%2Fprojects%2Fproject%2Flearning%3Ftab%3Dmodels%26archive%3Donly%26model_page%3D2%26model%3Dcandidate",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Navigation />
        <Routes>
          <Route
            path="/projects/:id/learning/lineage/:kind/:nodeId"
            element={<ModelLineagePage />}
          />
          <Route path="/projects/:id/learning/models/:modelId" element={<ModelPanelRoute />} />
          <Route path="*" element={<p>来源详情页</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("token-a", { id: "owner" } as never);
  vi.mocked(modelLineageApi.get).mockImplementation(async (_project, kind, id, relation) => ({
    ...result,
    node: { ...node, kind, id, facts: kind === "attempt" ? { run_id: "experiment" } : node.facts },
    relation,
    ...(relation === "predictions" ? { usage: { retained_count: 0, truncated: false } } : {}),
  }));
  vi.mocked(modelLineageApi.decisions).mockResolvedValue({ items: [] });
  vi.mocked(modelLineageApi.append).mockResolvedValue({ id: "decision" } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((c) => c.clear());
  useAuthStore.getState().logout();
});

describe("Model lineage", () => {
  it.each(["unknown", "constructor", "__proto__"])("rejects an invalid node kind %s", (kind) => {
    mount(`/projects/project/learning/lineage/${kind}/candidate`);
    expect(screen.getByRole("alert")).toHaveTextContent("无法识别这条记录的类型");
    expect(modelLineageApi.get).not.toHaveBeenCalled();
  });
  it("preserves archive/page return context and opens the exact historical attempt", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByRole("heading", { name: "雨天车辆候选" });
    expect(screen.getByTestId("location")).toHaveTextContent("/learning/models/candidate?");
    expect(screen.getByTestId("location")).toHaveTextContent("model_view=records");
    expect(decodeURIComponent(screen.getByTestId("location").textContent!)).toContain(
      "archive=only&model_page=2",
    );
    expect(screen.getByText("历史摘要缺失")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /执行尝试 1/ }));
    expect(screen.getByRole("link", { name: "返回来源页面" }).getAttribute("href")).toContain(
      "/learning/models/candidate?",
    );
    await waitFor(() =>
      expect(modelLineageApi.get).toHaveBeenCalledWith(
        "project",
        "attempt",
        "old-attempt",
        undefined,
        undefined,
        expect.anything(),
        20,
      ),
    );
    expect(await screen.findByRole("link", { name: /查看详情/ })).toHaveAttribute(
      "href",
      expect.stringContaining("tab=training&run=experiment&attempt=old-attempt&returnTo="),
    );
  });
  it("copies known lineage identities while keeping ordinary facts readable", async () => {
    const digest = "a".repeat(64);
    vi.mocked(modelLineageApi.get).mockResolvedValueOnce({
      ...result,
      node: {
        ...node,
        facts: {
          framework: "Ultralytics",
          ordinal: "3",
          checked_at: node.created_at,
          snapshot_digest: digest,
        },
      },
    });
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    mount();
    await screen.findByRole("heading", { name: "雨天车辆候选" });
    expect(screen.getByText("Ultralytics")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(new Date(node.created_at).toLocaleString("zh-CN"))).toBeInTheDocument();
    expect(screen.queryByText(digest)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "复制快照摘要" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(digest));
    expect(screen.getByRole("button", { name: "复制候选模型 ID" })).toBeInTheDocument();
  });
  it("keeps a request UUID after a lost acknowledgement and appends without publication calls", async () => {
    vi.mocked(modelLineageApi.append).mockRejectedValueOnce(new Error("响应中断"));
    const user = userEvent.setup();
    mount("/projects/project/learning/models/candidate?model_view=notes");
    await user.type(await screen.findByLabelText("标记说明"), "验证集较小，需要继续评估");
    await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
    await screen.findByText("响应中断");
    await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
    await screen.findByText("标记与说明已保存。默认预标选择未改变。");
    expect(vi.mocked(modelLineageApi.append).mock.calls[0][2]).toEqual(
      vi.mocked(modelLineageApi.append).mock.calls[1][2],
    );
    expect(vi.mocked(modelLineageApi.append).mock.calls[0][2]).toMatchObject({
      disposition: "research",
      note: "验证集较小，需要继续评估",
    });
  });
  it("drops a delayed decision completion after navigating away and back", async () => {
    let resolve!: (value: never) => void;
    vi.mocked(modelLineageApi.append).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    const user = userEvent.setup();
    mount("/projects/project/learning/models/candidate?model_view=notes");
    await user.type(await screen.findByLabelText("标记说明"), "旧查看位置的说明");
    await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
    await act(async () => navigate("?model_view=notes&before=2"));
    await waitFor(() =>
      expect(modelLineageApi.decisions).toHaveBeenCalledWith(
        "project",
        "candidate",
        2,
        expect.anything(),
        20,
      ),
    );
    await act(async () => navigate(-1));
    const invalidation = vi.spyOn(clients[0], "invalidateQueries");
    await act(async () => resolve({ id: "late" } as never));
    expect(screen.getByLabelText("标记说明")).toHaveValue("旧查看位置的说明");
    expect(screen.queryByText("标记与说明已保存。默认预标选择未改变。")).not.toBeInTheDocument();
    expect(invalidation).not.toHaveBeenCalled();
  });
  it("drops a late read after same-user token ABA", async () => {
    let resolve!: (value: LineageOut) => void;
    vi.mocked(modelLineageApi.get).mockImplementationOnce(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    mount();
    await waitFor(() => expect(modelLineageApi.get).toHaveBeenCalledTimes(1));
    await act(async () => {
      useAuthStore.getState().setToken("token-b");
      useAuthStore.getState().setToken("token-a");
    });
    await act(async () => resolve({ ...result, node: { ...node, name: "迟到的旧候选" } }));
    expect(screen.queryByText("迟到的旧候选")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "雨天车辆候选" })).toBeInTheDocument();
  });
  it("reports navigation cycles and fetches only the chosen related section", async () => {
    const user = userEvent.setup();
    mount("/projects/project/learning/lineage/model/candidate?trail=model%3Acandidate");
    await screen.findByText(/已回到之前查看过的记录/);
    await user.click(await screen.findByRole("button", { name: "使用此模型的预标注" }));
    await screen.findByText(/0 条记录不代表从未使用/);
    expect(modelLineageApi.get).toHaveBeenLastCalledWith(
      "project",
      "model",
      "candidate",
      "predictions",
      undefined,
      expect.anything(),
      20,
    );
    expect(screen.getByRole("button", { name: "下一页相关记录" })).toBeDisabled();
  });
  it("accepts only same-project learning return routes", () => {
    for (const target of [
      "https://elsewhere.invalid",
      "//elsewhere.invalid",
      "/projects/other/learning",
      "/projects/project/learning-evil",
      "\\evil",
    ])
      expect(lineageReturn("project", target)).toBe("/projects/project/learning?tab=models");
    expect(lineageReturn("project", "/projects/project/learning?tab=data&dataset=frozen")).toBe(
      "/projects/project/learning?tab=data&dataset=frozen",
    );
  });
  it("restores cursor history on the previous page and resets it when changing row count", async () => {
    vi.mocked(modelLineageApi.get).mockImplementation(
      async (_project, kind, id, relation, cursor) => ({
        ...result,
        node: { ...node, kind, id },
        relation,
        next_cursor: cursor ? undefined : "second-page",
      }),
    );
    const user = userEvent.setup();
    mount("/projects/project/learning/models/candidate?model_view=records&relation=evaluations");
    const next = await screen.findByRole("button", { name: "下一页相关记录" });
    await waitFor(() => expect(next).toBeEnabled());
    await user.click(next);
    await waitFor(() =>
      expect(modelLineageApi.get).toHaveBeenLastCalledWith(
        "project",
        "model",
        "candidate",
        "evaluations",
        "second-page",
        expect.anything(),
        20,
      ),
    );
    const previous = screen.getByRole("button", { name: "上一页相关记录" });
    await waitFor(() => expect(previous).toBeEnabled());
    await user.click(previous);
    await waitFor(() => expect(previous).toBeDisabled());
    await user.selectOptions(screen.getByLabelText("每页相关记录数"), "100");
    await waitFor(() =>
      expect(modelLineageApi.get).toHaveBeenLastCalledWith(
        "project",
        "model",
        "candidate",
        "evaluations",
        undefined,
        expect.anything(),
        100,
      ),
    );
    expect(previous).toBeDisabled();
    expect(screen.getByTestId("location")).not.toHaveTextContent("cursor=");
  });
});

it.each([
  ["active", "普通候选", /取消其他标记/],
  ["research", "待进一步评估", /不会自动创建评估任务/],
  ["declined", "暂不采用", /不会改变默认预标选择/],
  ["archived", "已归档", /不会删除权重或停用模型/],
] as const)(
  "explains the %s mark and keeps its stored value unchanged",
  async (value, label, explanation) => {
    const user = userEvent.setup();
    mount("/projects/project/learning/models/candidate?model_view=notes");
    const select = await screen.findByLabelText("模型标记");
    await user.selectOptions(select, value);
    expect(screen.getByRole("option", { name: label })).toHaveProperty("selected", true);
    expect(select).toHaveAccessibleDescription(explanation);
    await user.type(screen.getByLabelText("标记说明"), "记录本轮人工评估意见");
    await user.click(screen.getByRole("button", { name: "保存标记与说明" }));
    await waitFor(() =>
      expect(modelLineageApi.append).toHaveBeenCalledWith(
        "project",
        "candidate",
        expect.objectContaining({ disposition: value, note: "记录本轮人工评估意见" }),
      ),
    );
  },
);
