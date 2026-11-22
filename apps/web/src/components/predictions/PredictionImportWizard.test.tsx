import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { predictionsApi } from "@/api/predictions";

import { PredictionImportWizard } from "./PredictionImportWizard";

vi.mock("@/api/predictions", () => ({
  predictionsApi: {
    listByTask: vi.fn(),
    accept: vi.fn(),
    import: vi.fn(),
    importAnnotations: vi.fn(),
  },
}));

const importMock = predictionsApi.import as unknown as ReturnType<typeof vi.fn>;
const importAnnotationsMock = predictionsApi.importAnnotations as unknown as ReturnType<
  typeof vi.fn
>;

function makeFile(name = "test.json", content = "{}", type = "application/json"): File {
  return new File([content], name, { type });
}

describe("PredictionImportWizard", () => {
  beforeEach(() => {
    importMock.mockReset();
    importAnnotationsMock.mockReset();
  });

  it("初始 step 不允许在没有文件时预览", () => {
    render(<PredictionImportWizard open onClose={() => {}} projectId="p-1" />);
    const previewBtn = screen.getByRole("button", { name: /预览/ });
    expect(previewBtn).toBeDisabled();
  });

  it("预测导入默认勾选替换已有外部导入预测", () => {
    render(<PredictionImportWizard open onClose={() => {}} projectId="p-default" />);
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("选择文件后预览 → 显示 imported/skipped/errors 统计", async () => {
    importMock.mockResolvedValueOnce({
      imported: 5,
      skipped: 2,
      errors: [{ task_match: { display_id: "T-NOPE" }, reason: "task not found" }],
      dry_run: true,
    });

    render(<PredictionImportWizard open onClose={() => {}} projectId="p-1" />);

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [makeFile("aap.json")] },
    });

    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => {
      expect(importMock).toHaveBeenCalledWith(
        "p-1",
        "native_json",
        expect.any(File),
        expect.objectContaining({ overwriteExisting: true }),
        true,
      );
    });

    expect(await screen.findByText(/确认导入 5 条/)).toBeInTheDocument();
    expect(screen.getByText(/task not found/)).toBeInTheDocument();
  });

  it("格式切换为 COCO 后预览时使用 coco 参数", async () => {
    importMock.mockResolvedValueOnce({
      imported: 1,
      skipped: 0,
      errors: [],
      dry_run: true,
    });

    render(<PredictionImportWizard open onClose={() => {}} projectId="p-2" />);

    const select = screen.getByLabelText(/格式/) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "coco" } });

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [makeFile("c.json")] } });
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => {
      expect(importMock).toHaveBeenCalledWith(
        "p-2",
        "coco",
        expect.any(File),
        expect.any(Object),
        true,
      );
    });
  });

  it("COCO 默认尺寸填写后预览会透传 image_size_hint 字段", async () => {
    importMock.mockResolvedValueOnce({
      imported: 1,
      skipped: 0,
      errors: [],
      dry_run: true,
    });

    render(<PredictionImportWizard open onClose={() => {}} projectId="p-coco" />);

    fireEvent.change(screen.getByLabelText(/格式/), {
      target: { value: "coco" },
    });
    fireEvent.change(screen.getByPlaceholderText("宽度"), {
      target: { value: "1920" },
    });
    fireEvent.change(screen.getByPlaceholderText("高度"), {
      target: { value: "1080" },
    });

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [makeFile("coco.json")] } });
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => {
      expect(importMock).toHaveBeenCalledWith(
        "p-coco",
        "coco",
        expect.any(File),
        expect.objectContaining({ imageWidth: 1920, imageHeight: 1080 }),
        true,
      );
    });
  });

  it("格式切换为 YOLO 后预览会透传 zip 与 yoloVariant", async () => {
    importMock.mockResolvedValueOnce({
      imported: 1,
      skipped: 0,
      errors: [],
      dry_run: true,
    });

    render(<PredictionImportWizard open onClose={() => {}} projectId="p-yolo" />);

    fireEvent.change(screen.getByLabelText(/格式/), {
      target: { value: "yolo" },
    });
    fireEvent.change(screen.getByLabelText(/YOLO 变体/), {
      target: { value: "obb" },
    });

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [makeFile("labels.zip", "zip", "application/zip")] },
    });
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => {
      expect(importMock).toHaveBeenCalledWith(
        "p-yolo",
        "yolo",
        expect.any(File),
        expect.objectContaining({ yoloVariant: "obb" }),
        true,
      );
    });
  });

  it("多文件预览会一次请求后端并展示汇总错误", async () => {
    importMock.mockResolvedValueOnce({
      imported: 3,
      skipped: 1,
      errors: [{ task_match: { display_id: "T-NOPE" }, reason: "b.json: task not found" }],
      dry_run: true,
    });

    render(<PredictionImportWizard open onClose={() => {}} projectId="p-batch" />);

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [makeFile("a.json"), makeFile("b.json")] },
    });
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));

    await waitFor(() => expect(importMock).toHaveBeenCalledTimes(1));
    expect(importMock).toHaveBeenCalledWith(
      "p-batch",
      "native_json",
      expect.arrayContaining([expect.any(File), expect.any(File)]),
      expect.objectContaining({ overwriteExisting: true }),
      true,
    );
    expect(await screen.findByText(/确认导入 3 条/)).toBeInTheDocument();
    expect(screen.getByText(/b\.json: task not found/)).toBeInTheDocument();
  });

  it("预测导入向导不再承载标注导入切换", () => {
    render(<PredictionImportWizard open onClose={() => {}} projectId="p-anno" />);
    expect(screen.queryByLabelText(/导入对象/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/格式/)).toBeInTheDocument();
  });

  it("确认提交 → 调用 import 端点 dry_run=false 并触发 onComplete", async () => {
    importMock
      .mockResolvedValueOnce({
        imported: 3,
        skipped: 0,
        errors: [],
        dry_run: true,
      })
      .mockResolvedValueOnce({
        imported: 3,
        skipped: 0,
        errors: [],
        dry_run: false,
      });

    const onComplete = vi.fn();
    render(
      <PredictionImportWizard open onClose={() => {}} projectId="p-3" onComplete={onComplete} />,
    );

    const fileInput = document.getElementById("pi-file") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [makeFile("a.json")] } });
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));
    await waitFor(() => expect(importMock).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByRole("button", { name: /确认导入 3 条/ }));

    await waitFor(() => {
      expect(importMock).toHaveBeenCalledTimes(2);
      expect(importMock).toHaveBeenLastCalledWith(
        "p-3",
        "native_json",
        expect.any(File),
        expect.any(Object),
        false,
      );
    });
    expect(onComplete).toHaveBeenCalledTimes(1);
  });
});
