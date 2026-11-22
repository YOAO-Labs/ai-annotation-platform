import { afterEach, describe, expect, it, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { AnnotationResponse } from "@/types";
import type { AttributeSchema } from "@/api/projects";
import { ImageSelectionCardContent } from "./ImageSelectionCardContent";

function makeAnnotation(overrides: Partial<AnnotationResponse> = {}): AnnotationResponse {
  return {
    id: "anno-1",
    class_name: "car",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.25, h: 0.2 },
    attributes: {},
    ...overrides,
  } as AnnotationResponse;
}

const noop = () => {};

describe("ImageSelectionCardContent", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染 bbox 像素指标(尺寸 + 占图)", () => {
    const { getByText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation()}
        imageWidth={1920}
        imageHeight={1080}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
      />,
    );
    expect(getByText("480×216 px")).not.toBeNull();
    expect(getByText("5.0%")).not.toBeNull();
  });

  it("polygon 指标网格显示顶点数", () => {
    const ann = makeAnnotation({
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    });
    const { getByText } = render(
      <ImageSelectionCardContent
        annotation={ann}
        imageWidth={null}
        imageHeight={null}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
      />,
    );
    expect(getByText("顶点")).not.toBeNull();
    expect(getByText("4")).not.toBeNull();
  });

  it.each([
    {
      name: "polygon holes",
      geometry: {
        type: "polygon" as const,
        points: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ] as [number, number][],
        holes: [
          [
            [0.2, 0.2],
            [0.4, 0.2],
            [0.3, 0.4],
          ],
        ] as [number, number][][],
      },
    },
    {
      name: "multi_polygon",
      geometry: {
        type: "multi_polygon" as const,
        polygons: [
          {
            type: "polygon" as const,
            points: [
              [0, 0],
              [1, 0],
              [1, 1],
            ] as [number, number][],
          },
        ],
      },
    },
  ])("$name 显示防降级编辑提示", ({ geometry }) => {
    const { getByRole } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({ geometry })}
        imageWidth={100}
        imageHeight={100}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
      />,
    );

    expect(getByRole("status").textContent).toContain("禁用顶点编辑和整体拖动");
  });

  it("raster_mask 显示真实像素指标并保留管理操作", () => {
    const { getByRole, getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({
          geometry: {
            type: "raster_mask",
            mask: {
              encoding: "coco_rle_ref",
              size: [10, 20],
              object_key: "raster-masks/sha256/aa/bb/digest.json",
              sha256: "a".repeat(64),
              runs: 4,
              bytes: 32,
            },
          },
        })}
        imageWidth={20}
        imageHeight={10}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
        rasterMaskStatus={{
          state: "ready",
          cacheKey: "mask-v1",
          area: 17,
          componentCount: 2,
          holeCount: 1,
          boundaryPixelCount: 12,
          bounds: { x: 0.1, y: 0.2, w: 0.5, h: 0.4 },
          preview: false,
        }}
      />,
    );

    expect(getByRole("status").textContent).toContain(
      "已按真实像素渲染 · 17 px · 2 个组件 · 1 个孔洞 · 12 边界像素",
    );
    expect((getByLabelText("修改类别") as HTMLButtonElement).disabled).toBe(false);
    expect((getByLabelText("删除标注") as HTMLButtonElement).disabled).toBe(false);
  });

  it("raster_mask 错误保留后端原因到状态和复制诊断", () => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const { getByRole, getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({
          geometry: {
            type: "raster_mask",
            mask: {
              encoding: "coco_rle_ref",
              size: [10, 20],
              object_key: "raster-masks/sha256/aa/bb/digest.json",
              sha256: "a".repeat(64),
              runs: 4,
              bytes: 32,
            },
          },
        })}
        imageWidth={20}
        imageHeight={10}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
        rasterMaskStatus={{
          state: "error",
          reason: "corrupt",
          backendReason: "missing_object",
          message: "Mask object is missing",
          retryable: true,
          httpStatus: 409,
        }}
      />,
    );

    expect(getByRole("status").textContent).toContain("missing_object：Mask object is missing");
    fireEvent.click(getByLabelText("复制 Mask 诊断"));
    expect(writeText).toHaveBeenCalledWith(
      JSON.stringify({
        annotationId: "anno-1",
        reason: "missing_object",
        message: "Mask object is missing",
        httpStatus: 409,
      }),
    );
  });

  it("raster_mask 预算延后状态可从选中卡重试", () => {
    const onRetry = vi.fn();
    const { getByRole, getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({
          geometry: {
            type: "raster_mask",
            mask: {
              encoding: "coco_rle_ref",
              size: [10, 20],
              object_key: "raster-masks/sha256/aa/bb/digest.json",
              sha256: "a".repeat(64),
              runs: 4,
              bytes: 32,
            },
          },
        })}
        imageWidth={20}
        imageHeight={10}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
        onRetryRasterMask={onRetry}
        rasterMaskStatus={{
          state: "deferred",
          reason: "budget_exceeded",
          message: "Mask 已因当前缓存预算延后",
          retryable: true,
          requiredBytes: 2048,
          budgetBytes: 1024,
        }}
      />,
    );

    expect(getByRole("status").textContent).toContain("budget_exceeded：Mask 已因当前缓存预算延后");
    fireEvent.click(getByLabelText("重试 Mask 内容"));
    expect(onRetry).toHaveBeenCalledWith("anno-1");
  });

  it("改类 / 锁定 / 删除 回调透传正确参数", () => {
    const onChangeClass = vi.fn();
    const onToggleFlag = vi.fn();
    const onDelete = vi.fn();
    const { getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({ is_locked: false })}
        imageWidth={1920}
        imageHeight={1080}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={onChangeClass}
        onToggleFlag={onToggleFlag}
        onDelete={onDelete}
        onUpdateAttributes={noop}
      />,
    );
    fireEvent.click(getByLabelText("修改类别"));
    expect(onChangeClass).toHaveBeenCalledWith("anno-1");
    fireEvent.click(getByLabelText("锁定"));
    expect(onToggleFlag).toHaveBeenCalledWith("anno-1", "is_locked", true);
    fireEvent.click(getByLabelText("删除标注"));
    expect(onDelete).toHaveBeenCalledWith("anno-1");
  });

  it("有属性 schema 时渲染 AttributeForm,改值经 onUpdateAttributes 上抛", () => {
    const schema: AttributeSchema = {
      fields: [{ key: "color", label: "颜色", type: "text" }],
    };
    const onUpdateAttributes = vi.fn();
    const { getByDisplayValue } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({ attributes: { color: "red" } })}
        imageWidth={1920}
        imageHeight={1080}
        attributeSchema={schema}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={onUpdateAttributes}
      />,
    );
    const input = getByDisplayValue("red") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "blue" } });
    expect(input.value).toBe("blue");
  });

  it("readOnly 时禁用 改类/锁定/删除(隐藏仍可用)", () => {
    const { getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation()}
        imageWidth={1920}
        imageHeight={1080}
        attributeSchema={undefined}
        readOnly
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
      />,
    );
    expect((getByLabelText("修改类别") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("锁定") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("删除标注") as HTMLButtonElement).disabled).toBe(true);
  });

  it("对象锁定时禁用改类和删除，仍可解锁", () => {
    const { getByLabelText } = render(
      <ImageSelectionCardContent
        annotation={makeAnnotation({ is_locked: true })}
        imageWidth={1920}
        imageHeight={1080}
        attributeSchema={undefined}
        readOnly={false}
        onChangeClass={noop}
        onToggleFlag={noop}
        onDelete={noop}
        onUpdateAttributes={noop}
      />,
    );

    expect((getByLabelText("修改类别") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("删除标注") as HTMLButtonElement).disabled).toBe(true);
    expect((getByLabelText("解锁") as HTMLButtonElement).disabled).toBe(false);
  });
});
