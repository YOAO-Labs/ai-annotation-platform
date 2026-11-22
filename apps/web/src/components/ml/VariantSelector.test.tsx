import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VariantSelector } from "./VariantSelector";

describe("VariantSelector", () => {
  it("renders rich supported_variants metadata when available", () => {
    const onChange = vi.fn();
    render(
      <VariantSelector
        schema={{
          type: "object",
          properties: {
            sam_variant: {
              type: "string",
              enum: ["tiny", "small"],
              default: "tiny",
              title: "SAM 2 变体",
            },
          },
        }}
        supportedVariants={[
          {
            key: "sam_variant",
            title: "SAM 2",
            variants: [
              { value: "tiny", label: "SAM Tiny", vram_gb: 1.5, tier: "fast" },
              {
                value: "small",
                label: "SAM Small",
                vram_gb: 2.5,
                tier: "balanced",
                recommended: true,
                note: "推荐默认档",
              },
            ],
          },
        ]}
        value={{ sam_variant: "small" }}
        onChange={onChange}
      />,
    );

    expect(screen.getByTestId("ai-variant-selector")).toBeInTheDocument();
    expect(screen.getByText("显存约 2.5GB")).toBeInTheDocument();
    expect(screen.getByText("均衡")).toBeInTheDocument();
    expect(screen.getByText("推荐")).toBeInTheDocument();
    expect(screen.getByText("推荐默认档")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("ai-variant-sam_variant"), {
      target: { value: "tiny" },
    });
    expect(onChange).toHaveBeenCalledWith({ sam_variant: "tiny" });
  });

  it("falls back to params enum when rich metadata is absent", () => {
    render(
      <VariantSelector
        schema={{
          type: "object",
          properties: {
            dino_variant: {
              type: "string",
              enum: ["T", "B"],
              default: "T",
              title: "DINO 变体",
            },
          },
        }}
        value={{}}
        onChange={() => {}}
      />,
    );

    const select = screen.getByTestId("ai-variant-dino_variant") as HTMLSelectElement;
    expect(select.value).toBe("T");
    expect(screen.getByRole("option", { name: "B" })).toBeInTheDocument();
  });

  it("falls back to x-platform-role=modelVariant enum when rich metadata is absent", () => {
    render(
      <VariantSelector
        schema={{
          type: "object",
          properties: {
            model_variant: {
              type: "string",
              enum: ["sam3.1"],
              default: "sam3.1",
              "x-platform-role": "modelVariant",
            },
          },
        }}
        value={{}}
        onChange={() => {}}
      />,
    );

    const select = screen.getByTestId("ai-variant-model_variant") as HTMLSelectElement;
    expect(select.value).toBe("sam3.1");
  });

  it("renders nothing when no variant axis is declared", () => {
    const { container } = render(
      <VariantSelector
        schema={{
          type: "object",
          properties: {
            score_threshold: { type: "number", default: 0.5 },
          },
        }}
        supportedVariants={[]}
        value={{}}
        onChange={() => {}}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders arbitrary axis_keys from supportedVariants (yolo series/size)", () => {
    render(
      <VariantSelector
        supportedVariants={[
          {
            key: "series",
            title: "版本系列",
            variants: [
              { value: "yolov8", label: "YOLOv8" },
              { value: "yolo11", label: "YOLO11", recommended: true },
            ],
          },
          {
            key: "size",
            title: "尺寸",
            variants: [
              { value: "n", label: "nano" },
              { value: "s", label: "small", recommended: true },
            ],
          },
        ]}
        defaults={{ series: "yolo11", size: "s" }}
        value={{}}
        onChange={() => {}}
      />,
    );

    expect(screen.getByTestId("ai-variant-series")).toBeInTheDocument();
    expect(screen.getByTestId("ai-variant-size")).toBeInTheDocument();
    const series = screen.getByTestId("ai-variant-series") as HTMLSelectElement;
    const size = screen.getByTestId("ai-variant-size") as HTMLSelectElement;
    expect(series.value).toBe("yolo11");
    expect(size.value).toBe("s");
  });

  it("renders single-option axis for sam3 model_variant", () => {
    render(
      <VariantSelector
        supportedVariants={[
          {
            key: "model_variant",
            title: "模型版本",
            variants: [{ value: "sam3.1", label: "SAM 3.1", recommended: true }],
          },
        ]}
        defaults={{ model_variant: "sam3.1" }}
        value={{}}
        onChange={() => {}}
      />,
    );

    const select = screen.getByTestId("ai-variant-model_variant") as HTMLSelectElement;
    expect(select.value).toBe("sam3.1");
    expect(select.options.length).toBe(1);
  });

  it("filters second axis options by variantCombinations (yolo non-cartesian)", () => {
    render(
      <VariantSelector
        supportedVariants={[
          {
            key: "series",
            variants: [{ value: "yolov8" }, { value: "yolov9" }],
          },
          {
            key: "size",
            variants: [
              { value: "n" },
              { value: "s" },
              { value: "m" },
              { value: "c" },
              { value: "e" },
              { value: "x" },
            ],
          },
        ]}
        variantCombinations={[
          ["yolov8", "n"],
          ["yolov8", "s"],
          ["yolov8", "m"],
          ["yolov8", "x"],
          ["yolov9", "s"],
          ["yolov9", "m"],
          ["yolov9", "c"],
          ["yolov9", "e"],
        ]}
        value={{ series: "yolov9" }}
        onChange={() => {}}
      />,
    );

    const sizeSelect = screen.getByTestId("ai-variant-size") as HTMLSelectElement;
    const sizeValues = Array.from(sizeSelect.options).map((opt) => opt.value);
    expect(sizeValues).toEqual(["s", "m", "c", "e"]);
    expect(sizeValues).not.toContain("n");
    expect(sizeValues).not.toContain("x");
  });

  it("clears downstream axis when series change makes current size illegal", () => {
    const onChange = vi.fn();
    render(
      <VariantSelector
        supportedVariants={[
          { key: "series", variants: [{ value: "yolov8" }, { value: "yolov9" }] },
          { key: "size", variants: [{ value: "n" }, { value: "c" }] },
        ]}
        variantCombinations={[
          ["yolov8", "n"],
          ["yolov9", "c"],
        ]}
        value={{ series: "yolov8", size: "n" }}
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByTestId("ai-variant-series"), {
      target: { value: "yolov9" },
    });
    const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(lastCall.series).toBe("yolov9");
    expect(lastCall.size).toBeUndefined();
  });

  it("uses defaults over schema.default for initial value", () => {
    render(
      <VariantSelector
        schema={{
          type: "object",
          properties: {
            sam_variant: {
              type: "string",
              enum: ["tiny", "large"],
              default: "tiny",
            },
          },
        }}
        supportedVariants={[
          {
            key: "sam_variant",
            variants: [{ value: "tiny" }, { value: "large" }],
          },
        ]}
        defaults={{ sam_variant: "large" }}
        value={{}}
        onChange={() => {}}
      />,
    );

    const select = screen.getByTestId("ai-variant-sam_variant") as HTMLSelectElement;
    expect(select.value).toBe("large");
  });
});
