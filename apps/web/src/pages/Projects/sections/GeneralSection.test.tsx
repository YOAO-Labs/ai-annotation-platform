import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockUpdateMutate = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useUpdateProject: () => ({ mutate: mockUpdateMutate, isPending: false }),
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

import { GeneralSection } from "./GeneralSection";
import type { ProjectResponse } from "@/api/projects";

function makeProject(overrides: Partial<ProjectResponse> = {}): ProjectResponse {
  return {
    id: "p1",
    display_id: "P-1",
    name: "Demo Project",
    type_key: "image-det",
    type_label: "图像检测",
    status: "in_progress",
    due_date: null,
    classes: ["car", "person"],
    classes_config: null,
    ai_enabled: false,
    ml_backend_id: null,
    iou_dedup_threshold: 0.7,
    box_threshold: 0.35,
    text_threshold: 0.25,
    attribute_schema: null,
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
    created_at: "2026-05-09T00:00:00Z",
    updated_at: null,
    ...overrides,
  } as ProjectResponse;
}

function renderUI(project: ProjectResponse) {
  return render(
    <MemoryRouter>
      <GeneralSection project={project} />
    </MemoryRouter>,
  );
}

describe("GeneralSection", () => {
  beforeEach(() => {
    mockUpdateMutate.mockReset();
    mockPushToast.mockReset();
  });

  it("渲染初值: 项目名 / 状态 / 类型", () => {
    renderUI(makeProject());
    const nameInput = screen.getByDisplayValue("Demo Project") as HTMLInputElement;
    expect(nameInput).toBeInTheDocument();
    expect(screen.getByText("图像检测")).toBeInTheDocument();
    expect(screen.getByText("Scene 模式")).toBeInTheDocument();
    expect(screen.getByText("未开启")).toBeInTheDocument();
    expect(screen.queryByText("启用 AI 预标注")).not.toBeInTheDocument();
    expect(screen.queryByText("标注类别")).not.toBeInTheDocument();
  });

  it("scene_mode=true → 基本信息显示 Scene 模式已开启", () => {
    renderUI(makeProject({ scene_mode: true }));
    expect(screen.getByText("Scene 模式")).toBeInTheDocument();
    expect(screen.getByText("已开启")).toBeInTheDocument();
    expect(screen.getByText("按 scene 保持连续帧任务与批次边界")).toBeInTheDocument();
  });

  it("修改状态 → 即时保存只提交 status", () => {
    renderUI(makeProject());
    fireEvent.change(screen.getByDisplayValue("进行中"), {
      target: { value: "completed" },
    });
    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual({ status: "completed" });
  });

  it("图片项目可单独关闭原生 Mask 编辑", () => {
    renderUI(makeProject({ raster_mask_native_editing_enabled: true }));

    fireEvent.click(screen.getByRole("checkbox", { name: /原生 Mask 编辑/ }));

    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual({
      raster_mask_native_editing_enabled: false,
    });
  });

  it("旧响应缺少项目开关时按正式开启显示", () => {
    renderUI(makeProject());

    expect(screen.getByRole("checkbox", { name: /原生 Mask 编辑/ })).toBeChecked();
  });

  it("关闭的图片项目可重新开启原生 Mask 编辑", () => {
    renderUI(makeProject({ raster_mask_native_editing_enabled: false }));

    fireEvent.click(screen.getByRole("checkbox", { name: /原生 Mask 编辑/ }));

    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual({
      raster_mask_native_editing_enabled: true,
    });
  });

  it("空名失焦 → 弹 toast 并恢复, 不提交", () => {
    renderUI(makeProject());
    const nameInput = screen.getByDisplayValue("Demo Project") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "   " } });
    fireEvent.blur(nameInput);
    expect(mockUpdateMutate).not.toHaveBeenCalled();
    expect(mockPushToast).toHaveBeenCalledWith(
      expect.objectContaining({ msg: expect.stringContaining("项目名称不能为空") }),
    );
    expect(nameInput.value).toBe("Demo Project");
  });

  it("改名失焦 → 提交 trim 后的 name", () => {
    renderUI(makeProject());
    const nameInput = screen.getByDisplayValue("Demo Project") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "  Renamed  " } });
    fireEvent.blur(nameInput);
    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    const [payload] = mockUpdateMutate.mock.calls[0];
    expect(payload).toEqual({ name: "Renamed" });
  });

  it("名称未变化失焦 → 不提交", () => {
    renderUI(makeProject());
    const nameInput = screen.getByDisplayValue("Demo Project") as HTMLInputElement;
    fireEvent.blur(nameInput);
    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });
});
