import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render as rtlRender, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import type { MLModelCapability } from "@/api/ml-backends";
import type { AnnotationResponse } from "@/types";
import { SecondaryInferenceBar } from "./SecondaryInferenceBar";
import type { SecondaryCapability } from "../state/useSecondaryInference";

const pushToast = vi.fn();
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (sel: (s: { push: typeof pushToast }) => unknown) => sel({ push: pushToast }),
}));

const mutateAsync = vi.fn();
const capabilitiesRef: { current: SecondaryCapability[] } = { current: [] };
vi.mock("../state/useSecondaryInference", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../state/useSecondaryInference")>();
  return {
    ...actual,
    useSecondaryCapabilities: () => ({
      capabilities: capabilitiesRef.current,
      isLoading: false,
    }),
    useRunSecondaryInference: () => ({ mutateAsync }),
  };
});

function model(over: Partial<MLModelCapability>): MLModelCapability {
  return { id: "m1", display_name: "分类器", task: "classification", ...over } as MLModelCapability;
}
function attrCap(over: Partial<MLModelCapability> = {}): SecondaryCapability {
  return {
    backendId: "be-1",
    backendName: "onnx",
    model: model(over),
    writeTarget: "attributes",
    label: (over.display_name as string) || "分类器",
  };
}
function geomCap(): SecondaryCapability {
  return {
    backendId: "be-2",
    backendName: "yolo",
    model: model({ id: "det", display_name: "车牌检测", task: "detection" }),
    writeTarget: "geometry",
    label: "车牌检测",
  };
}

const annotation = {
  id: "anno-1",
  task_id: "task-1",
  class_name: "car",
} as AnnotationResponse;

let queryClient: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: queryClient }, children);
const render = (ui: Parameters<typeof rtlRender>[0]) => {
  const result = rtlRender(ui, { wrapper });
  const trigger = result.queryByTestId("secondary-settings-trigger");
  if (trigger) {
    fireEvent.click(trigger);
    fireEvent.click(result.getByRole("button", { name: "更多 二次推理 工具" }));
  }
  return result;
};

beforeEach(() => {
  pushToast.mockReset();
  mutateAsync.mockReset();
  capabilitiesRef.current = [];
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
});

describe("SecondaryInferenceBar", () => {
  it("keeps required text and Run usable while settings are collapsed", () => {
    capabilitiesRef.current = [attrCap({ supported_prompts: ["text"] })];
    const view = rtlRender(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
      { wrapper },
    );
    expect(view.queryByTestId("secondary-cap-select")).toBeNull();
    expect(view.getByTestId("secondary-run")).toBeDisabled();
    fireEvent.change(view.getByTestId("secondary-prompt"), { target: { value: "car" } });
    expect(view.getByTestId("secondary-run")).toBeEnabled();
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    fireEvent.click(view.getByRole("button", { name: "更多 二次推理 工具" }));
    expect(view.getByTestId("secondary-prompt")).toHaveValue("car");
    fireEvent.click(view.getByRole("button", { name: "收起二次推理设置" }));
    expect(view.getByTestId("secondary-prompt")).toHaveValue("car");
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("shares capsule confidence with full parameters and keeps other fields in the request", async () => {
    capabilitiesRef.current = [
      attrCap({
        params: {
          type: "object",
          properties: {
            custom_score: {
              type: "number",
              minimum: 0,
              maximum: 1,
              default: 0.4,
              "x-platform-role": "confidence",
            },
            iou: { type: "number", minimum: 0, maximum: 1, default: 0.6, "x-platform-role": "iou" },
          },
        },
      }),
      geomCap(),
    ];
    mutateAsync.mockResolvedValue({
      annotation: { ...annotation, attributes_meta: {} },
      created_children: [],
    });
    const view = rtlRender(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
      { wrapper },
    );
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    const quick = within(view.getByTestId("secondary-quick-confidence"));
    expect(quick.queryByTestId("schema-field-iou")).toBeNull();
    fireEvent.change(quick.getByRole("slider"), { target: { value: "0.8" } });
    fireEvent.click(view.getByRole("button", { name: "更多 二次推理 工具" }));
    fireEvent.click(view.getByTestId("secondary-params-toggle"));
    expect(within(view.getByTestId("schema-field-custom_score")).getByRole("slider")).toHaveValue(
      "0.8",
    );
    fireEvent.change(within(view.getByTestId("schema-field-iou")).getByRole("slider"), {
      target: { value: "0.7" },
    });
    fireEvent.change(view.getByTestId("secondary-cap-select"), { target: { value: "be-2:det" } });
    const close = view.getByRole("button", { name: "收起二次推理设置" });
    expect(close).toHaveTextContent("");
    fireEvent.click(close);
    expect(view.queryByTestId("secondary-quick-confidence")).toBeNull();
    expect(view.getByTestId("secondary-summary-capability")).toHaveTextContent("车牌检测");
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    fireEvent.click(view.getByRole("button", { name: "更多 二次推理 工具" }));
    fireEvent.change(view.getByTestId("secondary-cap-select"), { target: { value: "be-1:m1" } });
    fireEvent.click(view.getByRole("button", { name: "收起二次推理设置" }));
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    expect(within(view.getByTestId("secondary-quick-confidence")).getByRole("slider")).toHaveValue(
      "0.8",
    );
    expect(mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(view.getByTestId("secondary-run"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledOnce());
    expect(mutateAsync.mock.calls[0][0].body.params).toEqual({ custom_score: 0.8, iou: 0.7 });
  });

  it("only exposes editable numeric confidence fields, including legacy model keys", () => {
    capabilitiesRef.current = [
      attrCap({
        params: {
          type: "object",
          properties: {
            score_threshold: { type: "number", default: 0.4 },
            confidence: { type: "number", default: 0.5, readOnly: true },
            conf: { type: "string", default: "auto" },
            box_threshold: { type: "number", default: 0.3, "x-platform-role": "iou" },
          },
        },
      }),
    ];
    const view = rtlRender(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
      { wrapper },
    );
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    const quick = within(view.getByTestId("secondary-quick-confidence"));
    expect(quick.getByRole("spinbutton")).toHaveValue(0.4);
    expect(quick.queryByTestId("schema-field-confidence")).toBeNull();
    expect(quick.queryByTestId("schema-field-conf")).toBeNull();
    expect(quick.queryByTestId("schema-field-box_threshold")).toBeNull();
  });

  it("single-flights each annotation and lets old requests finish without changing the new owner", async () => {
    capabilitiesRef.current = [attrCap()];
    let finishA!: (value: unknown) => void;
    let finishB!: (value: unknown) => void;
    mutateAsync
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishA = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishB = resolve;
          }),
      );
    const view = rtlRender(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
      { wrapper },
    );
    const runA = view.getByTestId("secondary-run");
    act(() => {
      runA.click();
      runA.click();
    });
    expect(mutateAsync).toHaveBeenCalledOnce();
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      taskId: "task-1",
      annotationId: "anno-1",
    });
    const other = { ...annotation, id: "anno-2", task_id: "task-2" };
    view.rerender(<SecondaryInferenceBar projectId="p" taskId="task-2" annotation={other} />);
    fireEvent.click(view.getByTestId("secondary-run"));
    expect(mutateAsync).toHaveBeenCalledTimes(2);
    await act(async () => {
      finishA({ annotation: { ...annotation, attributes_meta: {} }, created_children: [] });
    });
    expect(pushToast).not.toHaveBeenCalled();
    expect(view.getByTestId("secondary-run")).toBeDisabled();
    view.rerender(
      <SecondaryInferenceBar projectId="p" taskId="task-2" annotation={other} presentationHidden />,
    );
    expect(view.queryByTestId("secondary-settings-trigger")).toBeNull();
    view.rerender(<SecondaryInferenceBar projectId="p" taskId="task-2" annotation={other} />);
    expect(view.getByTestId("secondary-run")).toBeDisabled();
    await act(async () => {
      finishB({ annotation: { ...other, attributes_meta: {} }, created_children: [] });
    });
    expect(view.getByTestId("secondary-run")).toBeEnabled();
    expect(pushToast).toHaveBeenCalledOnce();
  });

  it("retains the selected capability and values through a failed request and collapse", async () => {
    capabilitiesRef.current = [
      attrCap(),
      { ...geomCap(), model: { ...geomCap().model, supported_prompts: ["text"] } },
    ];
    mutateAsync.mockRejectedValue(new Error("离线"));
    const view = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    fireEvent.change(view.getByTestId("secondary-cap-select"), { target: { value: "be-2:det" } });
    fireEvent.change(view.getByTestId("secondary-prompt"), { target: { value: "car" } });
    fireEvent.click(view.getByTestId("secondary-run"));
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "error" })),
    );
    fireEvent.click(view.getByRole("button", { name: "收起二次推理设置" }));
    expect(view.getByTestId("secondary-prompt")).toHaveValue("car");
    expect(view.getByTestId("secondary-run")).toBeEnabled();
    fireEvent.click(view.getByTestId("secondary-settings-trigger"));
    fireEvent.click(view.getByRole("button", { name: "更多 二次推理 工具" }));
    expect(view.getByTestId("secondary-cap-select")).toHaveValue("be-2:det");
    expect(mutateAsync).toHaveBeenCalledOnce();
  });
  it("无能力 → 不渲染", () => {
    const { container } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("readOnly → 不渲染 (即便有能力)", () => {
    capabilitiesRef.current = [attrCap()];
    const { container } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} readOnly />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("有能力 → 渲染能力下拉 + 运行按钮", () => {
    capabilitiesRef.current = [attrCap(), geomCap()];
    const { getByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    const select = getByTestId("secondary-cap-select") as HTMLSelectElement;
    expect(select.querySelectorAll("option")).toHaveLength(2);
    expect(getByTestId("secondary-run")).toBeTruthy();
  });

  it("有变体轴的能力 → 渲染档位下拉; 无变体轴 → 无", () => {
    capabilitiesRef.current = [
      attrCap(),
      {
        backendId: "be-2",
        backendName: "yolo",
        model: model({
          id: "det",
          display_name: "车牌检测",
          task: "detection",
          supported_variants: [
            {
              key: "size",
              variants: [{ value: "s" }, { value: "l" }],
            },
          ] as MLModelCapability["supported_variants"],
        }),
        writeTarget: "geometry",
        label: "车牌检测",
      },
    ];
    const { getByTestId, queryByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    expect(queryByTestId("ai-variant-size")).toBeNull();
    fireEvent.change(getByTestId("secondary-cap-select"), {
      target: { value: "be-2:det" },
    });
    expect(getByTestId("ai-variant-size")).toBeTruthy();
  });

  it("OCR 属性能力有变体轴 → 也渲染档位下拉 (不再只认几何)", () => {
    capabilitiesRef.current = [
      attrCap({
        id: "ocr-rec",
        display_name: "文本识别",
        task: "ocr",
        supported_variants: [
          { key: "lang", variants: [{ value: "universal" }, { value: "en" }] },
        ] as MLModelCapability["supported_variants"],
      }),
    ];
    const { getByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    expect(getByTestId("ai-variant-lang")).toBeTruthy();
  });

  it("开集文本能力 → 出现文本框; 空文本禁运行, 填后可运行并带 prompt", async () => {
    capabilitiesRef.current = [
      {
        backendId: "be-3",
        backendName: "gsam2",
        model: model({
          id: "gdino",
          display_name: "开集检测",
          task: "detection",
          supported_prompts: ["text"],
        }),
        writeTarget: "geometry",
        label: "开集检测",
      },
    ];
    mutateAsync.mockResolvedValue({
      annotation: { ...annotation, attributes_meta: {} },
      created_children: [{ id: "c1" }],
    });
    const { getByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    const input = getByTestId("secondary-prompt") as HTMLInputElement;
    expect(input).toBeTruthy();
    expect((getByTestId("secondary-run") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "car . person" } });
    expect((getByTestId("secondary-run") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(getByTestId("secondary-run"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].body.prompt).toBe("car . person");
  });

  it("非开集能力 → 无文本框", () => {
    capabilitiesRef.current = [geomCap()];
    const { queryByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    expect(queryByTestId("secondary-prompt")).toBeNull();
  });

  it("选几何能力后运行 → 调 run + 新增子框 toast", async () => {
    capabilitiesRef.current = [attrCap(), geomCap()];
    mutateAsync.mockResolvedValue({
      annotation: { ...annotation, attributes_meta: {} },
      created_children: [{ id: "c1" }, { id: "c2" }],
    });
    const { getByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    fireEvent.change(getByTestId("secondary-cap-select"), {
      target: { value: "be-2:det" },
    });
    fireEvent.click(getByTestId("secondary-run"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({ sub: "新增 2 个子框", kind: "success" }),
      ),
    );
  });

  it("attributes 缺承接字段 → 渲染补全 CTA, 点击回调带缺失字段", () => {
    capabilitiesRef.current = [
      attrCap({
        output_attribute_schema: [
          { key: "color", label: "颜色", type: "select", options: [] },
        ] as MLModelCapability["output_attribute_schema"],
      }),
    ];
    const onEnsure = vi.fn();
    const { getByTestId } = render(
      <SecondaryInferenceBar
        projectId="p"
        taskId="task-1"
        annotation={annotation}
        existingAttributeKeys={new Set()}
        onEnsureAttributeFields={onEnsure}
      />,
    );
    fireEvent.click(getByTestId("secondary-fill"));
    expect(onEnsure).toHaveBeenCalledWith([
      expect.objectContaining({ key: "color", label: "颜色" }),
    ]);
  });

  it("attributes 已有承接字段 → 无补全 CTA", () => {
    capabilitiesRef.current = [
      attrCap({
        output_attribute_schema: [
          { key: "color", label: "颜色", type: "select", options: [] },
        ] as MLModelCapability["output_attribute_schema"],
      }),
    ];
    const { queryByTestId } = render(
      <SecondaryInferenceBar
        projectId="p"
        taskId="task-1"
        annotation={annotation}
        existingAttributeKeys={new Set(["color"])}
        onEnsureAttributeFields={vi.fn()}
      />,
    );
    expect(queryByTestId("secondary-fill")).toBeNull();
  });

  it("选中能力有可调参数 → 显示 ⚙, 点击展开参数面板; 无参数 → 无 ⚙", () => {
    capabilitiesRef.current = [
      attrCap({
        id: "withp",
        params: {
          type: "object",
          properties: { score_threshold: { type: "number", default: 0.5 } },
        },
      }),
    ];
    const { getByTestId, queryByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    const toggle = getByTestId("secondary-params-toggle");
    expect(queryByTestId("secondary-params-panel")).toBeNull();
    fireEvent.click(toggle);
    expect(getByTestId("secondary-params-panel")).toBeTruthy();
  });

  it("选中能力无可调参数 → 无 ⚙", () => {
    capabilitiesRef.current = [attrCap({ id: "nop" })];
    const { queryByTestId } = render(
      <SecondaryInferenceBar projectId="p" taskId="task-1" annotation={annotation} />,
    );
    expect(queryByTestId("secondary-params-toggle")).toBeNull();
  });

  it("运行属性能力写了缺字段的键 → warning toast 提示不显示", async () => {
    capabilitiesRef.current = [attrCap()];
    mutateAsync.mockResolvedValue({
      annotation: { ...annotation, attributes_meta: { color: { origin: "ai" } } },
      created_children: [],
    });
    const { getByTestId } = render(
      <SecondaryInferenceBar
        projectId="p"
        taskId="task-1"
        annotation={annotation}
        existingAttributeKeys={new Set()}
      />,
    );
    fireEvent.click(getByTestId("secondary-run"));
    await waitFor(() =>
      expect(pushToast).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "warning",
          sub: expect.stringContaining("缺字段"),
        }),
      ),
    );
  });
});
