import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { trainedModelsApi } from "@/api/trainedModels";
import { encodeCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import { ProjectModelMaskTrial } from "./ProjectModelMaskTrial";

vi.mock("@/api/trainedModels", async (original) => ({
  ...(await original<typeof import("@/api/trainedModels")>()),
  trainedModelsApi: { trialMaskContent: vi.fn() },
}));
const alpha = Uint8Array.from([255, 255, 255, 255, 0, 255, 255, 255, 255]);
const rle = encodeCocoRle(alpha, 3, 3);
const bitmap = vi.fn(async (_data: ImageData) => ({ close: vi.fn() }));
const drawImage = vi.fn();
const trial = {
  sample_url: "/fixed-image.png",
  inference_time_ms: 12,
  result: [
    {
      tool_unit_id: "region",
      class_name: "车辆",
      geometry: {
        type: "raster_mask",
        mask: {
          encoding: "coco_rle_ref",
          size: [3, 3],
          sha256: "a".repeat(64),
          object_key: "private-object",
          bytes: 60,
          runs: 7,
        },
      },
      score: 0.9,
      native_class_index: 0,
      instance_index: 0,
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(trainedModelsApi.trialMaskContent).mockResolvedValue(rle);
  vi.stubGlobal(
    "ImageData",
    class {
      data: Uint8ClampedArray;
      width: number;
      height: number;
      constructor(data: Uint8ClampedArray, width: number, height: number) {
        this.data = data;
        this.width = width;
        this.height = height;
      }
    },
  );
  vi.stubGlobal("createImageBitmap", bitmap);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    clearRect: vi.fn(),
    drawImage,
  } as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount(value = trial, onReady = vi.fn()) {
  render(
    <ProjectModelMaskTrial
      projectId="project"
      modelId="model"
      deploymentId="deployment"
      trial={value}
      onReady={onReady}
    />,
  );
  fireEvent.load(screen.getByAltText("冻结样本与逐实例掩码"));
  return onReady;
}

describe("frozen segmentation trial", () => {
  it("fetches the owning deployment instance and renders the hole as transparent pixels", async () => {
    const onReady = mount();
    await waitFor(() => expect(onReady).toHaveBeenLastCalledWith(true));
    expect(trainedModelsApi.trialMaskContent).toHaveBeenCalledWith(
      "project",
      "model",
      "deployment",
      0,
    );
    expect(screen.getByAltText("冻结样本与逐实例掩码")).toHaveAttribute("src", "/fixed-image.png");
    const data = bitmap.mock.calls[0][0] as unknown as ImageData;
    expect(data.width).toBe(3);
    expect(data.height).toBe(3);
    expect(Array.from(data.data).filter((_, index) => index % 4 === 3)).toEqual(Array.from(alpha));
    expect(drawImage).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps confirmation blocked on a failed mask fetch and supports explicit retry", async () => {
    vi.mocked(trainedModelsApi.trialMaskContent).mockRejectedValueOnce(
      new Error("冻结掩码暂不可用"),
    );
    const user = userEvent.setup();
    const onReady = mount();
    await screen.findByText("Mask 内容加载失败");
    expect(onReady).not.toHaveBeenCalledWith(true);
    await user.click(screen.getByRole("button", { name: "重试掩码" }));
    await waitFor(() => expect(onReady).toHaveBeenLastCalledWith(true));
    expect(trainedModelsApi.trialMaskContent).toHaveBeenCalledTimes(2);
  });

  it("rejects unsupported polygon trial output instead of treating it as a mask", async () => {
    const onReady = mount({
      ...trial,
      result: [{ ...trial.result[0], geometry: { type: "polygon" } }],
    } as never);
    await screen.findByText("试用结果包含无效的实例掩码引用，请重新加载模型");
    expect(trainedModelsApi.trialMaskContent).not.toHaveBeenCalled();
    expect(onReady).not.toHaveBeenCalledWith(true);
  });
});
