/**
 * ClassesSection — 同名类跨工具单位批量重命名开关。
 *
 * 覆盖: 多 unit 同名类时开关出现 / 默认仅改当前 unit / 开启后走跨 unit 批量路径
 * (不传 tool_unit_id) / 单 unit 独占时开关不渲染。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockRenameMutate = vi.fn();
const mockUpdateMutate = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useUpdateProject: () => ({ mutate: mockUpdateMutate, isPending: false }),
  useRenameClass: () => ({ mutate: mockRenameMutate, isPending: false }),
}));
vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

import { ClassesSection } from "./ClassesSection";
import type { ProjectResponse } from "@/api/projects";

function makeProject(toolBindings: Record<string, unknown>): ProjectResponse {
  return {
    id: "p1",
    display_id: "P-1",
    name: "Demo",
    type_key: "image-det",
    type_label: "图像检测",
    data_type: "image",
    status: "in_progress",
    due_date: null,
    classes: [],
    classes_config: null,
    attribute_schema: null,
    tool_bindings: toolBindings,
    ai_enabled: false,
    ml_backend_id: null,
    iou_dedup_threshold: 0.7,
    box_threshold: 0.35,
    text_threshold: 0.25,
    description: null,
    completed_count: 0,
    total_tasks_count: 0,
    pending_review_count: 0,
    in_review_count: 0,
    rejected_count: 0,
    members_count: 0,
    pre_annotated_batch_count: 0,
    annotating_batch_count: 0,
    review_batch_count: 0,
    completed_batch_count: 0,
    archived_batch_count: 0,
    created_at: "2026-06-22T00:00:00Z",
    updated_at: null,
  } as unknown as ProjectResponse;
}

const sharedBindings = {
  bbox: {
    enabled: true,
    classes: [{ name: "person", color: "#ff0000", order: 0 }],
    attribute_schema: { fields: [] },
  },
  region: {
    enabled: true,
    classes: [{ name: "person", color: "#ff0000", order: 0 }],
    attribute_schema: { fields: [] },
  },
};

function renderUI(project: ProjectResponse) {
  // 包 QueryClientProvider: ClassesSection 内挂的 PrefillFromBackendDialog
  // 调 useCapabilityInstances(useQuery), 即使 dialog 关闭 hook 也无条件挂载。生产
  // 环境整个 App 在 QueryClient 包装下, 测试须对齐。
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ClassesSection project={project} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renameFirstPersonTo(newName: string) {
  const input = screen.getByDisplayValue("person");
  fireEvent.change(input, { target: { value: newName } });
  fireEvent.blur(input);
}

beforeEach(() => {
  mockRenameMutate.mockClear();
  mockUpdateMutate.mockClear();
  mockPushToast.mockClear();
});

describe("ClassesSection 跨工具单位批量重命名", () => {
  it("默认关闭: 重命名仅改当前工具单位 (传 tool_unit_id)", () => {
    renderUI(makeProject(sharedBindings));
    renameFirstPersonTo("human");
    expect(mockRenameMutate).toHaveBeenCalledTimes(1);
    expect(mockRenameMutate.mock.calls[0][0]).toMatchObject({
      old_name: "person",
      new_name: "human",
      tool_unit_id: "bbox",
    });
  });

  it("开启后: 重命名走跨 unit 批量路径 (不传 tool_unit_id)", () => {
    renderUI(makeProject(sharedBindings));
    fireEvent.click(screen.getByTestId("rename-all-units-switch"));
    renameFirstPersonTo("human");
    expect(mockRenameMutate).toHaveBeenCalledTimes(1);
    expect(mockRenameMutate.mock.calls[0][0].tool_unit_id).toBeUndefined();
    expect(mockRenameMutate.mock.calls[0][0]).toMatchObject({
      old_name: "person",
      new_name: "human",
    });
  });

  it("无跨单位同名类时, 不渲染批量开关", () => {
    renderUI(
      makeProject({
        bbox: {
          enabled: true,
          classes: [{ name: "person", color: "#ff0000", order: 0 }],
          attribute_schema: { fields: [] },
        },
        region: {
          enabled: true,
          classes: [{ name: "tree", color: "#00ff00", order: 0 }],
          attribute_schema: { fields: [] },
        },
      }),
    );
    expect(screen.queryByTestId("rename-all-units-switch")).not.toBeInTheDocument();
  });
});
