import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { sampleEvaluationsApi, type EvaluationSampleResult } from "@/api/sampleEvaluations";
import { useAuthStore } from "@/stores/authStore";
import { encodeCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import { SampleEvaluationImage } from "./SampleEvaluationImage";

vi.mock("@/api/sampleEvaluations", () => ({ sampleEvaluationsApi: { image: vi.fn() } }));
const alpha = Uint8Array.from([
  255, 255, 255, 0, 0, 255, 0, 255, 0, 0, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255,
]);
const rle = encodeCocoRle(alpha, 5, 5);
const pixelEvidence = {
  index: 0,
  class_index: 0,
  rle,
  mask_sha256: "a".repeat(64),
  area_pixels: 9,
};
const sample = {
  ordinal: 0,
  task_id: "task",
  sha256: "b".repeat(64),
  label_sha256: "c".repeat(64),
  width: 5,
  height: 5,
  status: "succeeded",
  truth: [
    {
      ...pixelEvidence,
      annotation_id: "annotation",
      annotation_version: 1,
      rasterizer: "native-rle-1",
    },
  ],
  predictions: [{ ...pixelEvidence, native_class_index: 0, score: 0.8 }],
  request_id: "request",
  evidence_digest: "d".repeat(64),
} satisfies EvaluationSampleResult;
const bitmap = vi.fn(async (_data: ImageData) => ({ close: vi.fn() }));
const drawImage = vi.fn();
const originalCreate = URL.createObjectURL,
  originalRevoke = URL.revokeObjectURL;

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("mask-evaluation", { id: "owner" } as never);
  vi.mocked(sampleEvaluationsApi.image).mockResolvedValue(
    new Blob(["frozen"], { type: "image/png" }),
  );
  URL.createObjectURL = vi.fn().mockReturnValue("blob:mask-evaluation");
  URL.revokeObjectURL = vi.fn();
  vi.stubGlobal(
    "ImageData",
    class {
      constructor(
        public data: Uint8ClampedArray,
        public width: number,
        public height: number,
      ) {}
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
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});
function mount(value: EvaluationSampleResult = sample) {
  return render(
    <SampleEvaluationImage
      projectId="project"
      evaluationId="evaluation"
      attemptId="attempt"
      sample={value}
      classes={["车辆"]}
      scope="mask-test"
      task="segment"
    />,
  );
}

describe("frozen mask evaluation pixels", () => {
  it("decodes the actual hole and disconnected foreground for both layers and clears hidden layers", async () => {
    const view = mount();
    await screen.findByAltText("冻结样本与逐实例掩码");
    await waitFor(() => expect(bitmap).toHaveBeenCalledTimes(2));
    for (const [image] of bitmap.mock.calls) {
      expect(image.width).toBe(5);
      expect(image.height).toBe(5);
      expect(Array.from(image.data).filter((_, index) => index % 4 === 3)).toEqual(
        Array.from(alpha),
      );
    }
    expect(drawImage).toHaveBeenCalled();
    expect(view.container.querySelector("[data-layer]")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "真值掩码，蓝色" }));
    await waitFor(() => expect(bitmap).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("checkbox", { name: "预测掩码，橙色" }));
    expect(screen.getByRole("checkbox", { name: "真值掩码，蓝色" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "预测掩码，橙色" })).not.toBeChecked();
    expect(view.container.querySelector("canvas")).not.toHaveAttribute("width");
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mask-evaluation");
  });

  it("reports invalid original dimensions and never substitutes rectangles", async () => {
    const view = mount({ ...sample, width: 4 });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "冻结掩码尺寸或像素面积与证据不一致",
    );
    expect(bitmap).not.toHaveBeenCalled();
    expect(view.container.querySelector("[data-layer]")).toBeNull();
  });
});
