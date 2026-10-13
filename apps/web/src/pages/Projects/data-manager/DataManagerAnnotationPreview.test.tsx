import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnnotationResponse } from "@/types";
import type { ToolBindings } from "@/api/projects";

const state = vi.hoisted(() => ({
  task: null as Record<string, unknown> | null,
  pages: [] as Array<{ items: Partial<AnnotationResponse>[]; next_cursor: string | null }>,
  getTask: vi.fn(),
  getAnnotationsPage: vi.fn(),
  maskContent: vi.fn(),
  getImagePyramid: vi.fn(),
  retryImagePyramid: vi.fn(),
  toolBindings: {} as ToolBindings,
  failMediaLoads: 0,
  mediaLoads: [] as string[],
  maskBudget: undefined as number | undefined,
}));

vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({
    data: {
      id: "p1",
      name: "Preview project",
      tool_bindings: state.toolBindings,
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    get: (...args: unknown[]) => state.getTask(...args),
    getAnnotationsPage: (...args: unknown[]) => state.getAnnotationsPage(...args),
    getImagePyramid: (...args: unknown[]) => state.getImagePyramid(...args),
    retryImagePyramid: (...args: unknown[]) => state.retryImagePyramid(...args),
  },
}));

vi.mock("@/api/rasterMasks", () => ({
  rasterMasksApi: {
    annotationRasterMaskContent: (...args: unknown[]) => state.maskContent(...args),
  },
}));

vi.mock("@/pages/Workbench/stage/shared/useRasterMaskRecords", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/pages/Workbench/stage/shared/useRasterMaskRecords")>();
  return {
    ...actual,
    useRasterMaskRecords: (options: Parameters<typeof actual.useRasterMaskRecords>[0]) =>
      actual.useRasterMaskRecords({ ...options, maxCacheBytes: state.maskBudget }),
  };
});

import DataManagerAnnotationPreview from "./DataManagerAnnotationPreview";

function annotation(overrides: Partial<AnnotationResponse>): AnnotationResponse {
  return {
    id: "a1",
    task_id: "t1",
    project_id: "p1",
    user_id: "u1",
    source: "manual",
    annotation_type: "bbox",
    class_name: "car",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.4, h: 0.3 },
    confidence: null,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    created_at: "2026-09-10T00:00:00Z",
    updated_at: null,
    ...overrides,
  } as AnnotationResponse;
}

function resetState() {
  class PreviewImage extends EventTarget {
    naturalWidth = 800;
    naturalHeight = 600;
    decode = vi.fn().mockResolvedValue(undefined);
    private value = "";
    get src() {
      return this.value;
    }
    set src(value: string) {
      this.value = value;
      if (!value) return;
      state.mediaLoads.push(value);
      const event = state.failMediaLoads > 0 ? "error" : "load";
      state.failMediaLoads = Math.max(0, state.failMediaLoads - 1);
      queueMicrotask(() => this.dispatchEvent(new Event(event)));
    }
  }
  state.failMediaLoads = 0;
  state.mediaLoads = [];
  state.maskBudget = undefined;
  vi.stubGlobal("Image", PreviewImage);
  class PreviewBitmap {
    close = vi.fn();
  }
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
  vi.stubGlobal("ImageBitmap", PreviewBitmap);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => new PreviewBitmap()),
  );
  state.toolBindings = {
    bbox: { enabled: true, classes: [{ name: "car", color: "#ff0000", order: 0 }] },
    keypoint: {
      enabled: true,
      classes: [{ name: "person", color: "#00ff00", order: 1 }],
      keypoint_schema: {
        nodes: [{ name: "head", color: "#0000ff" }, { name: "foot" }],
        edges: [[0, 1]],
      },
    },
  };
  state.task = {
    id: "t1",
    project_id: "p1",
    file_type: "image",
    file_url: "http://media.example/t1.png",
    thumbnail_url: null,
    blurhash: null,
    image_width: 800,
    image_height: 600,
    image_pyramid: null,
  };
  state.pages = [];
  state.getTask.mockReset();
  state.getAnnotationsPage.mockReset();
  state.maskContent.mockReset();
  state.getImagePyramid.mockReset();
  state.retryImagePyramid.mockReset();
  state.getTask.mockResolvedValue(state.task);
  state.getAnnotationsPage.mockResolvedValue({ items: [], next_cursor: null });
  state.maskContent.mockResolvedValue({ encoding: "coco_rle", size: [10, 10], counts: [0, 100] });
}

function createClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPreview(
  props: Partial<Parameters<typeof DataManagerAnnotationPreview>[0]> = {},
  client = createClient(),
  strict = false,
) {
  const preview = <DataManagerAnnotationPreview projectId="p1" taskId="t1" {...props} />;
  return render(
    <QueryClientProvider client={client}>
      {strict ? <StrictMode>{preview}</StrictMode> : preview}
    </QueryClientProvider>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function maskAnnotation(id: string, isHidden = false) {
  return annotation({
    id,
    tool_unit_id: "region",
    annotation_type: "raster_mask",
    is_hidden: isHidden,
    geometry: {
      type: "raster_mask",
      mask: {
        encoding: "coco_rle_ref",
        size: [10, 10],
        object_key: `masks/${id}`,
        sha256: id,
        runs: 1,
        bytes: 10,
      },
    },
  });
}

function pageResponse(page: { items: Partial<AnnotationResponse>[]; next_cursor: string | null }) {
  return {
    items: page.items.map(annotation),
    next_cursor: page.next_cursor,
  };
}

describe("DataManagerAnnotationPreview", () => {
  beforeEach(resetState);
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("observes the container when uncached media finishes loading and after resize", async () => {
    const taskRequest = deferred<typeof state.task>();
    state.getTask.mockReturnValue(taskRequest.promise);
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(380);
    let resize!: ResizeObserverCallback;
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          resize = callback;
        }
        observe = vi.fn();
        disconnect = disconnect;
      },
    );

    const preview = renderPreview();
    expect(screen.getByLabelText("标注预览").querySelector("[data-konva='Stage']")).toBeNull();
    await act(async () => taskRequest.resolve(state.task));
    await screen.findByText("已保存标注 0 条");
    const stage = screen.getByLabelText("标注预览").querySelector("[data-konva='Stage']");
    expect(stage).toHaveAttribute("data-width", "380");
    width.mockReturnValue(420);
    act(() => resize([], {} as ResizeObserver));
    expect(stage).toHaveAttribute("data-width", "420");
    preview.unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it("distinguishes pending and failed annotations from an empty successful result", async () => {
    const request = deferred<ReturnType<typeof pageResponse>>();
    state.getAnnotationsPage.mockReturnValueOnce(request.promise);
    renderPreview({ highlightAnnotationId: "wanted" });
    await waitFor(() => expect(state.getAnnotationsPage).toHaveBeenCalledTimes(1));
    expect(screen.getByText("正在加载已保存标注…")).toBeInTheDocument();
    expect(screen.queryByText(/已保存标注 0 条|可能已被删除/)).not.toBeInTheDocument();

    await act(async () => request.reject(new Error("annotations unavailable")));
    expect(await screen.findByRole("alert")).toHaveTextContent("标注加载失败");
    expect(screen.queryByText(/已保存标注 0 条|可能已被删除/)).not.toBeInTheDocument();

    state.getAnnotationsPage.mockResolvedValueOnce({ items: [], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "重试加载标注" }));
    expect(await screen.findByText("已保存标注 0 条")).toBeInTheDocument();
    expect(screen.getByText(/未找到选中的对象/)).toBeInTheDocument();
  });

  it("pauses automatic object lookup after a failed page and resumes on explicit retry", async () => {
    const failedPage = deferred<ReturnType<typeof pageResponse>>();
    const accidentalRetry = deferred<ReturnType<typeof pageResponse>>();
    state.getAnnotationsPage
      .mockResolvedValueOnce(pageResponse({ items: [{ id: "first" }], next_cursor: "next" }))
      .mockReturnValueOnce(failedPage.promise)
      .mockReturnValue(accidentalRetry.promise);
    renderPreview({ highlightAnnotationId: "wanted" });
    await waitFor(() => expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2));
    await act(async () => failedPage.reject(new Error("next page unavailable")));
    expect(await screen.findByRole("alert")).toHaveTextContent("后续标注加载失败");
    expect(screen.getByText("已保存标注 1 条")).toBeInTheDocument();
    expect(screen.queryByText(/可能已被删除/)).not.toBeInTheDocument();
    expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2);

    state.getAnnotationsPage.mockResolvedValueOnce(
      pageResponse({ items: [{ id: "wanted" }], next_cursor: null }),
    );
    fireEvent.click(screen.getByRole("button", { name: "重试加载更多标注" }));
    await screen.findByText("已保存标注 2 条");
    expect(state.getAnnotationsPage).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps identically named class colors scoped to each annotation tool", async () => {
    state.toolBindings.region = {
      enabled: true,
      classes: [{ name: "car", color: "#0000ff", order: 0 }],
    };
    state.getAnnotationsPage.mockResolvedValue(
      pageResponse({
        items: [
          { id: "red-box", tool_unit_id: "bbox" },
          {
            id: "blue-polygon",
            tool_unit_id: "region",
            annotation_type: "polygon",
            geometry: {
              type: "polygon",
              points: [
                [0, 0],
                [1, 0],
                [0, 1],
              ],
            },
          },
        ],
        next_cursor: null,
      }),
    );
    renderPreview();
    await screen.findByText("已保存标注 2 条");
    const section = screen.getByLabelText("标注预览");
    expect(section.querySelector("[data-id='red-box'] [data-konva='Rect']")).toHaveAttribute(
      "data-stroke",
      "#ff0000",
    );
    expect(section.querySelector("[data-id='blue-polygon'] [data-konva='Line']")).toHaveAttribute(
      "data-stroke",
      "#0000ff",
    );
  });

  it("renders style_occluded annotations dashed and translucent like the Workbench", async () => {
    state.toolBindings.bbox = {
      enabled: true,
      classes: [{ name: "car", color: "#ff0000", order: 0 }],
      attribute_schema: {
        fields: [
          { key: "occluded", label: "遮挡", type: "boolean", style_occluded: true },
          { key: "unrelated", label: "其他", type: "boolean", style_occluded: false },
        ],
      },
    };
    state.getAnnotationsPage.mockResolvedValue(
      pageResponse({
        items: [
          { id: "box-solid" },
          { id: "box-occluded", attributes: { occluded: true } },
          { id: "box-flag-off", attributes: { occluded: false } },
        ],
        next_cursor: null,
      }),
    );

    renderPreview();
    await screen.findByText("已保存标注 3 条");
    const section = screen.getByLabelText("标注预览");
    const solid = section.querySelector("[data-id='box-solid'] [data-konva='Rect']");
    const occluded = section.querySelector("[data-id='box-occluded'] [data-konva='Rect']");
    const flagOff = section.querySelector("[data-id='box-flag-off'] [data-konva='Rect']");
    expect(solid).not.toBeNull();
    expect(occluded).not.toBeNull();
    expect(flagOff).not.toBeNull();
    // Occluded matches the Workbench visual: dashed stroke + half opacity.
    expect(occluded!.hasAttribute("data-dash")).toBe(true);
    expect(Number(occluded!.getAttribute("data-opacity"))).toBeCloseTo(0.5);
    // Without the occluding attribute (or with it false) boxes stay solid.
    expect(solid!.hasAttribute("data-dash")).toBe(false);
    expect(solid!.getAttribute("data-opacity")).toBe("1");
    expect(flagOff!.hasAttribute("data-dash")).toBe(false);
    expect(flagOff!.getAttribute("data-opacity")).toBe("1");
  });

  it("revalidates saved annotations immediately when a cached preview reopens", async () => {
    const client = createClient();
    state.getAnnotationsPage.mockResolvedValueOnce({
      items: [annotation({ id: "a1" })],
      next_cursor: null,
    });
    const props = { projectId: "p1", taskId: "t1", highlightAnnotationId: "a1" };
    const first = renderPreview(props, client);
    await screen.findByText("已保存标注 1 条");
    first.unmount();

    // A saved deletion in the Workbench must be visible even within the
    // previous preview's cache freshness window.
    renderPreview(props, client);
    await waitFor(() => expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2));
    await screen.findByText("已保存标注 0 条");
    expect(screen.getByText(/未找到选中的对象/)).toBeInTheDocument();
  });

  it("retries a failed image even when refreshed task metadata returns the same URL", async () => {
    state.failMediaLoads = 2;
    renderPreview();
    await waitFor(() => expect(state.mediaLoads).toHaveLength(2));
    fireEvent.click(await screen.findByRole("button", { name: "重新加载预览" }));
    await waitFor(() => expect(state.mediaLoads).toHaveLength(3));
    expect(new Set(state.mediaLoads).size).toBe(1);
    await screen.findByRole("button", { name: "隐藏标注" });
    expect(screen.queryByText(/图片加载失败/)).not.toBeInTheDocument();
  });

  it("waits for cached annotation revalidation before searching later pages", async () => {
    const client = createClient();
    state.getAnnotationsPage.mockResolvedValueOnce({
      items: [annotation({ id: "old" })],
      next_cursor: "old-cursor",
    });
    const first = renderPreview({}, client);
    await screen.findByText("已保存标注 1 条");
    first.unmount();
    const refresh = deferred<{ items: AnnotationResponse[]; next_cursor: null }>();
    state.getAnnotationsPage.mockImplementation(
      (_id: string, options: { cursor: string | null }) =>
        options.cursor === null
          ? refresh.promise
          : Promise.resolve({ items: [], next_cursor: null }),
    );
    renderPreview({ highlightAnnotationId: "new" }, client);
    const button = await screen.findByRole("button", { name: "加载更多标注" });
    expect(button).toBeDisabled();
    expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2);
    const signal = state.getAnnotationsPage.mock.calls[1]?.[2]?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await act(async () =>
      refresh.resolve({ items: [annotation({ id: "new" })], next_cursor: null }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "加载更多标注" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/未找到选中的对象/)).not.toBeInTheDocument();
    expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2);
  });

  it("reports masks deferred by the memory budget as incomplete rendering", async () => {
    state.maskBudget = 1;
    state.getAnnotationsPage.mockResolvedValue({
      items: [maskAnnotation("budget-mask")],
      next_cursor: null,
    });
    renderPreview();
    await screen.findByText(/1 个掩码因内存预算暂未显示/);
    expect(screen.queryByTestId("raster-mask-fill")).not.toBeInTheDocument();
  });

  it("refreshes failed pyramid metadata without requesting a rebuild", async () => {
    state.getTask.mockResolvedValue({
      ...state.task,
      image_width: 8192,
      image_height: 8192,
      image_pyramid: { status: "failed", generation: 2, required: true, width: 8192, height: 8192 },
    });
    state.getImagePyramid.mockResolvedValue({ status: "failed", required: true, retryable: true });
    renderPreview();
    const reload = await screen.findByRole("button", { name: "重新加载切片状态" });
    await waitFor(() => expect(state.getImagePyramid).toHaveBeenCalledTimes(1));
    fireEvent.click(reload);
    await waitFor(() => expect(state.getImagePyramid).toHaveBeenCalledTimes(2));
    expect(state.retryImagePyramid).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "cancels the first cached mask load on close (StrictMode=%s)",
    async (strict) => {
      const client = createClient();
      const mask = maskAnnotation(`cached-mask-${strict}`);
      // Warm the actual query cache through the same read path, with the mask
      // hidden so reopening is the first mask-content request.
      state.getAnnotationsPage.mockResolvedValue({
        items: [{ ...mask, is_hidden: true }],
        next_cursor: null,
      });
      const warm = renderPreview({}, client);
      await screen.findByText("已保存标注 1 条");
      warm.unmount();
      const cacheKey = client
        .getQueryCache()
        .getAll()
        .find((query) => query.queryKey[0] === "dm-preview-annotations")!.queryKey;
      client.setQueryData(cacheKey, {
        pages: [{ items: [mask], next_cursor: null }],
        pageParams: [null],
      });
      state.getAnnotationsPage.mockResolvedValue({ items: [mask], next_cursor: null });
      state.maskContent.mockClear();
      const requests: Array<{
        signal?: AbortSignal;
        request: ReturnType<
          typeof deferred<{ encoding: string; size: number[]; counts: number[] }>
        >;
      }> = [];
      state.maskContent.mockImplementation((_id: string, options?: { signal?: AbortSignal }) => {
        const request = deferred<{ encoding: string; size: number[]; counts: number[] }>();
        requests.push({ signal: options?.signal, request });
        options?.signal?.addEventListener(
          "abort",
          () => request.reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
        return request.promise;
      });
      const preview = renderPreview({}, client, strict);
      await waitFor(() => expect(state.maskContent).toHaveBeenCalled());
      const signal = requests[0]?.signal;
      expect(signal).toBeInstanceOf(AbortSignal);
      await waitFor(() => expect(requests.some((item) => !item.signal?.aborted)).toBe(true));
      const active = requests.find((item) => !item.signal?.aborted)!;
      await act(async () =>
        active.request.resolve({ encoding: "coco_rle", size: [10, 10], counts: [0, 100] }),
      );
      expect(await screen.findByTestId("raster-mask-fill")).toBeInTheDocument();
      expect(screen.queryByText(/个掩码加载失败/)).not.toBeInTheDocument();
      preview.unmount();
      expect(active.signal?.aborted).toBe(true);
      client.clear();
    },
  );

  it("does not fetch or draw hidden masks while showing visible masks", async () => {
    state.getAnnotationsPage.mockResolvedValue({
      items: [maskAnnotation("visible-mask"), maskAnnotation("hidden-mask", true)],
      next_cursor: null,
    });
    renderPreview();
    await screen.findByText("已保存标注 2 条");
    await waitFor(() =>
      expect(state.maskContent).toHaveBeenCalledWith("visible-mask", expect.anything()),
    );
    expect(state.maskContent.mock.calls.map(([id]) => id)).toEqual(["visible-mask"]);
    expect(await screen.findByTestId("raster-mask-fill")).toBeInTheDocument();
    expect(screen.getByLabelText("标注预览").querySelector("[data-id='hidden-mask']")).toBeNull();
  });

  it("renders saved shapes with explicit project colors and alignment", async () => {
    state.pages = [
      {
        items: [
          { id: "box-1", geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.4, h: 0.3 } },
          {
            id: "poly-1",
            annotation_type: "polygon",
            class_name: "car",
            geometry: {
              type: "polygon",
              points: [
                [0.1, 0.1],
                [0.5, 0.1],
                [0.5, 0.5],
                [0.1, 0.5],
              ],
              holes: [
                [
                  [0.2, 0.2],
                  [0.3, 0.2],
                  [0.3, 0.3],
                ],
              ],
            },
          },
          {
            id: "rot-1",
            geometry: { type: "rotated_bbox", cx: 0.6, cy: 0.6, w: 0.2, h: 0.1, angle: 30 },
          },
          {
            id: "line-1",
            annotation_type: "polyline",
            geometry: {
              type: "polyline",
              points: [
                [0.1, 0.8],
                [0.4, 0.9],
              ],
            },
          },
          {
            id: "kp-1",
            tool_unit_id: "keypoint",
            class_name: "person",
            annotation_type: "keypoint",
            geometry: {
              type: "keypoint",
              points: [
                { x: 0.2, y: 0.2, v: 2 },
                { x: 0.4, y: 0.5, v: 2 },
              ],
            },
          },
          {
            id: "mask-1",
            annotation_type: "raster_mask",
            geometry: {
              type: "raster_mask",
              mask: {
                encoding: "coco_rle_ref",
                size: [10, 10],
                object_key: "masks/mask-1",
                sha256: "abc",
                runs: 10,
                bytes: 10,
              },
            },
          },
        ],
        next_cursor: null,
      },
    ];
    state.getAnnotationsPage.mockResolvedValue(pageResponse(state.pages[0]));

    renderPreview();
    await screen.findByText("已保存标注 6 条");
    const section = screen.getByLabelText("标注预览");

    // Image pixels and geometry share one transform: Stage width/height follow
    // the fitted image box.
    const stage = section.querySelector("[data-konva='Stage']") as HTMLElement | null;
    expect(stage).not.toBeNull();

    // Vector shapes carry the explicit project class color, not a global one.
    const rectNodes = section.querySelectorAll("[data-konva='Rect']");
    expect(rectNodes.length).toBeGreaterThan(0);
    const withCarColor = Array.from(rectNodes).filter(
      (node) => node.getAttribute("data-stroke") === "#ff0000",
    );
    expect(withCarColor.length).toBeGreaterThan(0);

    // Polygon holes use the even-odd branch.
    expect(section.querySelector("[data-fillrule='evenodd']")).not.toBeNull();

    // Rotated box keeps its angle on the rotating group itself.
    const rotated = Array.from(section.querySelectorAll("[data-konva='Group']")).find(
      (node) => node.getAttribute("data-rotation") === "30",
    );
    expect(rotated).toBeTruthy();

    // Keypoint skeleton edges come from the project binding.
    const edgeLines = Array.from(section.querySelectorAll("[data-konva='Line']"));
    expect(edgeLines.some((line) => (line.getAttribute("data-points") ?? "").includes("160"))).toBe(
      true,
    );

    // Mask content is fetched through the cancellable adapter.
    await waitFor(() => expect(state.maskContent).toHaveBeenCalled());
    expect(state.maskContent.mock.calls[0][0]).toBe("mask-1");
    expect(state.maskContent.mock.calls[0][1]).toMatchObject({ signal: expect.anything() });
  });

  it("toggles saved annotations off for comparing with the original image", async () => {
    state.pages = [
      {
        items: [{ id: "box-1", geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.4, h: 0.3 } }],
        next_cursor: null,
      },
    ];
    state.getAnnotationsPage.mockResolvedValue(pageResponse(state.pages[0]));

    renderPreview();
    await screen.findByText("已保存标注 1 条");
    const section = screen.getByLabelText("标注预览");
    expect(section.querySelectorAll("[data-konva='Rect']").length).toBeGreaterThan(0);

    fireEvent.click(within(section).getByRole("button", { name: /隐藏标注/ }));
    await waitFor(() => expect(section.querySelectorAll("[data-konva='Rect']").length).toBe(0));
    // The image itself stays visible.
    expect(within(section).getByRole("button", { name: /显示标注/ })).toBeInTheDocument();
  });

  it("loads annotation pages progressively with an explicit control", async () => {
    state.pages = [
      { items: [{ id: "box-1" }], next_cursor: "cursor-2" },
      { items: [{ id: "box-2" }], next_cursor: null },
    ];
    state.getAnnotationsPage.mockResolvedValueOnce(pageResponse(state.pages[0]));

    renderPreview();
    expect(await screen.findByText("已保存标注 1 条")).toBeInTheDocument();
    expect(screen.getByText("部分加载")).toBeInTheDocument();

    state.getAnnotationsPage.mockResolvedValueOnce(pageResponse(state.pages[1]));
    fireEvent.click(screen.getByRole("button", { name: "加载更多标注" }));
    await screen.findByText("已保存标注 2 条");
    expect(screen.queryByText("部分加载")).not.toBeInTheDocument();
    expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2);
  });

  it("keeps walking pages until the highlighted object is found", async () => {
    state.pages = [
      { items: [{ id: "box-1" }], next_cursor: "cursor-2" },
      {
        items: [{ id: "wanted", geometry: { type: "bbox", x: 0.2, y: 0.2, w: 0.2, h: 0.2 } }],
        next_cursor: null,
      },
    ];
    state.getAnnotationsPage
      .mockResolvedValueOnce(pageResponse(state.pages[0]))
      .mockResolvedValueOnce(pageResponse(state.pages[1]));

    renderPreview({ highlightAnnotationId: "wanted" });
    // Second page is fetched automatically because the highlight is absent
    // from the first page.
    await waitFor(() => expect(state.getAnnotationsPage).toHaveBeenCalledTimes(2));
    await screen.findByText("已保存标注 2 条");
    expect(screen.queryByText(/未找到选中的对象/)).not.toBeInTheDocument();
  });

  it("reports a missing highlighted object only after pages are exhausted", async () => {
    state.pages = [{ items: [{ id: "box-1" }], next_cursor: null }];
    state.getAnnotationsPage.mockResolvedValue(pageResponse(state.pages[0]));

    renderPreview({ highlightAnnotationId: "deleted" });
    await screen.findByText(/未找到选中的对象，可能已被删除/);
  });

  it("renders nothing for non-image media", async () => {
    state.task = { ...state.task, file_type: "video" };
    state.getTask.mockResolvedValue(state.task);

    const { container } = renderPreview();
    await waitFor(() => expect(state.getTask).toHaveBeenCalled());
    // Wait for the query state to settle before asserting absence.
    await waitFor(() => expect(container.querySelector("[aria-label='标注预览']")).toBeNull());
    expect(state.getAnnotationsPage).not.toHaveBeenCalled();
  });

  it("offers an actionable retry after a media failure and refreshes once", async () => {
    state.pages = [{ items: [{ id: "box-1" }], next_cursor: null }];
    state.getAnnotationsPage.mockResolvedValue(pageResponse(state.pages[0]));
    // Force the media load to fail by removing every media URL.
    state.task = { ...state.task, file_url: null, thumbnail_url: null };
    state.getTask.mockResolvedValue(state.task);

    renderPreview();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("图片加载失败");
    // The retry refetches task metadata for a fresh signed URL.
    fireEvent.click(screen.getByRole("button", { name: "重新加载预览" }));
    await waitFor(() => expect(state.getTask.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});
