/**
 * CreateProjectWizard 单测
 * 覆盖: 初始渲染 step1 / step1 名称校验 / step1→2→3→4 推进 / step4 提交触发 mutation /
 *       提交成功进入 step5 / sourceProjectId 复制模式横幅
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import * as stageColors from "@/pages/Workbench/stage/colors";

const mockCreateProjectMutate = vi.fn();
const mockCreateProjectReset = vi.fn();
const mockPushToast = vi.fn();
const mockProjectsApiGet = vi.fn();
const mockProjectTemplatesApiGet = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useCreateProject: () => ({
    mutate: mockCreateProjectMutate,
    reset: mockCreateProjectReset,
    isPending: false,
  }),
  useAddProjectMember: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock("@/api/projects", async () => {
  const actual = await vi.importActual<any>("@/api/projects");
  return {
    ...actual,
    projectsApi: {
      ...actual.projectsApi,
      get: (...args: unknown[]) => mockProjectsApiGet(...args),
    },
  };
});

vi.mock("@/api/projectTemplates", async () => {
  const actual = await vi.importActual<any>("@/api/projectTemplates");
  return {
    ...actual,
    projectTemplatesApi: {
      ...((actual as any).projectTemplatesApi ?? {}),
      get: (...args: unknown[]) => mockProjectTemplatesApiGet(...args),
    },
  };
});

vi.mock("@/api/adminMlIntegrations", () => ({
  adminMlIntegrationsApi: {
    listAll: vi.fn().mockResolvedValue([]),
  },
}));

// Step5 / Step6 use datasetsApi / usersApi — mock those hooks/apis used by subcomponents
vi.mock("@/hooks/useDatasets", () => ({
  useDatasets: () => ({ data: { items: [] } }),
  useLinkProject: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useBatches", () => ({
  useSplitBatches: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useUsers", () => ({
  useUsers: () => ({ data: [] }),
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { CreateProjectWizard } from "./CreateProjectWizard";

function renderUI(props: Partial<React.ComponentProps<typeof CreateProjectWizard>> = {}) {
  const defaults = {
    open: true,
    onClose: vi.fn(),
  };
  return render(
    <MemoryRouter>
      <CreateProjectWizard {...defaults} {...props} />
    </MemoryRouter>,
  );
}

describe("CreateProjectWizard", () => {
  afterEach(() => vi.restoreAllMocks());

  beforeEach(() => {
    mockCreateProjectMutate.mockReset();
    mockCreateProjectReset.mockReset();
    mockPushToast.mockReset();
    mockProjectsApiGet.mockReset().mockResolvedValue({
      id: "src1",
      name: "Source",
      type_key: "image-det",
      data_type: "image",
      ai_enabled: false,
      classes: [],
      classes_config: {},
      tool_bindings: {},
    });
    mockProjectTemplatesApiGet.mockReset().mockResolvedValue({
      id: "t1",
      name: "Template",
      type_key: "image-det",
      data_type: "image",
      ai_enabled: false,
      classes: [],
      classes_config: {},
      tool_bindings: {},
    });
  });

  it("初始渲染: 显示「新建项目」标题 + step1 表单 + 下一步按钮禁用", () => {
    renderUI();
    expect(screen.getByText("新建项目")).toBeInTheDocument();
    // Step1 内容
    expect(screen.getByPlaceholderText(/智能门店货架/)).toBeInTheDocument();
    const nextBtn = screen.getByRole("button", { name: /下一步/ });
    expect(nextBtn).toBeDisabled();
  });

  it("step1 名称 1 字符时「下一步」仍禁用", () => {
    renderUI();
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "A" },
    });
    expect(screen.getByRole("button", { name: /下一步/ })).toBeDisabled();
  });

  it("step1 填入有效名称后「下一步」启用并点击进入 step2", () => {
    renderUI();
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "测试项目名称" },
    });
    const nextBtn = screen.getByRole("button", { name: /下一步/ });
    expect(nextBtn).not.toBeDisabled();
    fireEvent.click(nextBtn);
    // step2 — 类别步骤
    expect(screen.getByText("类别")).toBeInTheDocument();
  });

  it("step1→2→3→4 逐步推进, step4 显示「创建」按钮", () => {
    renderUI();
    // step1 → step2
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "完整流程项目" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    // step2 → step3
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    // step3 → step4
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    // step4: 「创建」按钮出现
    expect(screen.getByRole("button", { name: /^创建$/ })).toBeInTheDocument();
  });

  it("step4 点「创建」→ 调用 createProject.mutate", () => {
    renderUI();
    // 快速推进到 step4
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "提交测试项目" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));
    expect(mockCreateProjectMutate).toHaveBeenCalledTimes(1);
    const payload = mockCreateProjectMutate.mock.calls[0][0];
    expect(payload.name).toBe("提交测试项目");
  });

  it("提交成功后 onSuccess 回调显示 step5 (数据集步骤)", async () => {
    renderUI();
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "成功回调项目" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));

    // 模拟 mutate onSuccess 回调
    const onSuccess = mockCreateProjectMutate.mock.calls[0][1]?.onSuccess;
    if (onSuccess) {
      await onSuccess({
        id: "p-new",
        display_id: "P-100",
        name: "成功回调项目",
        type_key: "image-det",
        data_type: "image",
      });
    }
    await waitFor(() =>
      expect(mockPushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success" })),
    );
  });

  it("sourceProjectId 给定时显示「复制项目配置」横幅", async () => {
    renderUI({ sourceProjectId: "src1" });
    await waitFor(() => expect(screen.getByText(/已用源项目配置预填表单/)).toBeInTheDocument());
    expect(screen.getByText("复制项目配置")).toBeInTheDocument();
  });

  it.each([
    { mode: "source", legacy: false },
    { mode: "source", legacy: true },
    { mode: "template", legacy: false },
    { mode: "template", legacy: true },
  ])("$mode legacy=$legacy 保留显式颜色并用类别默认色预填及提交", async ({ mode, legacy }) => {
    // Hex fixtures exercise the real editor without relying on jsdom's canvas parser.
    vi.spyOn(stageColors, "classColor").mockImplementation((name) =>
      name === "person" ? "#123456" : "#abcdef",
    );
    const classes = [
      { name: "car", color: "#ff0000", order: 2 },
      { name: "bicycle", order: 1 },
      { name: "person", order: 0, alias: "pedestrian" },
      { name: "motorcycle", color: "#888888", order: 3 },
    ];
    const source = {
      name: "Source",
      type_key: "image-det",
      data_type: "image",
      ai_enabled: false,
      ...(legacy
        ? {
            classes: classes.map((c) => c.name),
            classes_config: Object.fromEntries(
              classes.map(({ name, ...config }) => [name, config]),
            ),
          }
        : { tool_bindings: { bbox: { enabled: true, classes } } }),
    };
    mockProjectsApiGet.mockResolvedValue(source);
    mockProjectTemplatesApiGet.mockResolvedValue(source);
    renderUI(mode === "source" ? { sourceProjectId: "src1" } : { templateId: "t1" });
    await screen.findByText(mode === "source" ? /已用源项目配置预填表单/ : /已用模板字段预填表单/);
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "颜色复制测试" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

    const colorInputs = screen
      .getByRole("dialog")
      .querySelectorAll<HTMLInputElement>("input[type=color]");
    expect(Array.from(colorInputs, (input) => input.value)).toEqual([
      "#123456",
      "#abcdef",
      "#ff0000",
      "#888888",
    ]);
    expect(screen.getByDisplayValue("pedestrian")).toBeInTheDocument();
    fireEvent.change(colorInputs[1], { target: { value: "#654321" } });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));
    const payload = mockCreateProjectMutate.mock.calls[0][0];
    expect(payload.tool_bindings.bbox.classes).toEqual([
      { name: "person", color: "#123456", order: 0, alias: "pedestrian" },
      { name: "bicycle", color: "#654321", order: 1 },
      { name: "car", color: "#ff0000", order: 2 },
      { name: "motorcycle", color: "#888888", order: 3 },
    ]);
    expect(payload.classes_config.person).toEqual({
      color: "#123456",
      order: 0,
      alias: "pedestrian",
    });
    expect(payload.classes_config.bicycle).toEqual({ color: "#654321", order: 1 });
  });

  it("open=false 时不渲染任何内容", () => {
    renderUI({ open: false });
    expect(screen.queryByText("新建项目")).not.toBeInTheDocument();
  });
});
