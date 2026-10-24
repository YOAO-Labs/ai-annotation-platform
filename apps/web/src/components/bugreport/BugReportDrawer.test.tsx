import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import { BugReportDrawer } from "./BugReportDrawer";

const mocks = vi.hoisted(() => ({
  pushToast: vi.fn(),
  listMine: vi.fn(),
  get: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  addComment: vi.fn(),
  upload: vi.fn(),
  download: vi.fn(),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: <T,>(selector: (s: { push: typeof mocks.pushToast }) => T) =>
    selector({ push: mocks.pushToast }),
}));

vi.mock("@/components/markdown/MarkdownEditor", () => ({
  MarkdownEditor: ({
    value,
    onChange,
    onSubmit,
    onBlur,
    onUploadImage,
    placeholder,
    documentId,
    label,
    variant,
    disabled,
  }: {
    value: string;
    onChange: (next: string) => void;
    onSubmit?: (next: string) => void;
    onBlur?: () => void;
    onUploadImage?: unknown;
    placeholder?: string;
    documentId?: string;
    label?: string;
    variant?: string;
    disabled?: boolean;
  }) => (
    <textarea
      data-testid="markdown-editor"
      data-document-id={documentId}
      data-label={label}
      data-variant={variant}
      data-has-upload={onUploadImage ? "true" : "false"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSubmit?.(value);
      }}
      onBlur={onBlur}
      placeholder={placeholder}
      disabled={disabled}
    />
  ),
}));

vi.mock("@/utils/bugReportCapture", () => ({
  getRecentApiCalls: () => [],
  getRecentConsoleErrors: () => [],
  sanitizeApiCalls: (calls: unknown[]) => calls,
  captureScreenshot: vi.fn(),
}));

vi.mock("@/api/bug-reports", () => ({
  bugReportsApi: {
    listMine: mocks.listMine,
    get: mocks.get,
    create: mocks.create,
    update: mocks.update,
    delete: vi.fn(),
    addComment: mocks.addComment,
    downloadAttachment: mocks.download,
  },
  uploadBugAttachment: mocks.upload,
}));

describe("BugReportDrawer", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("drawer-token", { id: "drawer-owner" } as never);
    mocks.upload.mockReset();
    mocks.download.mockReset();
    mocks.pushToast.mockClear();
    mocks.listMine.mockResolvedValue({ items: [], total: 0 });
    mocks.get.mockReset();
    mocks.create.mockReset();
    mocks.create.mockResolvedValue({});
    mocks.update.mockReset().mockResolvedValue({});
    mocks.addComment.mockReset().mockResolvedValue({});
    delete (window as unknown as { __videoWorkbenchDiagnostics?: unknown })
      .__videoWorkbenchDiagnostics;
    delete (window as unknown as { __videoFrameClockDiagnostics?: unknown })
      .__videoFrameClockDiagnostics;
    delete (window as unknown as { __rasterMaskComputeDiagnostics?: unknown })
      .__rasterMaskComputeDiagnostics;
  });

  it("adds pasted clipboard screenshots and allows removing them", async () => {
    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    const textarea = await screen.findByPlaceholderText("详细描述问题...");
    const file = new File(["image"], "clip.png", { type: "image/png" });
    const secondFile = new File(["image-2"], "clip-2.webp", { type: "image/webp" });

    fireEvent.paste(textarea, {
      clipboardData: {
        files: [file, secondFile],
      },
    });

    expect(await screen.findByText(/图 1/)).toBeInTheDocument();
    expect(screen.getByText(/clip\.png/)).toBeInTheDocument();
    expect(screen.getByText(/图 2/)).toBeInTheDocument();
    expect(screen.getByText(/clip-2\.webp/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByText("移除")[0]);

    expect(screen.queryByText(/clip\.png/)).not.toBeInTheDocument();
  });

  it("uses the compact editor without inline comment or attachment uploads", async () => {
    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));

    const editor = await screen.findByPlaceholderText("详细描述问题...");
    expect(editor).toHaveAttribute("data-variant", "compact");
    expect(editor).toHaveAttribute("data-document-id", "bug-create");
    expect(editor).toHaveAttribute("data-label", "反馈描述");
    expect(editor).toHaveAttribute("data-has-upload", "false");
  });

  it("accepts the description boundary using Python-compatible Unicode length", async () => {
    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "Unicode boundary" },
    });
    fireEvent.change(await screen.findByPlaceholderText("详细描述问题..."), {
      target: { value: "😀".repeat(20_000) },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    expect(mocks.create.mock.calls[0][0].description).toBe("😀".repeat(20_000));
  });

  it("rejects an oversized description while retaining the draft", async () => {
    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "Too long" },
    });
    const editor = await screen.findByPlaceholderText("详细描述问题...");
    const description = "😀".repeat(20_001);
    fireEvent.change(editor, { target: { value: description } });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.pushToast).toHaveBeenCalledWith({
      msg: "描述（含自动诊断）不能超过 20000 个字符",
      kind: "error",
    });
    expect(editor).toHaveValue(description);
  });

  it("rejects oversized pasted screenshots", async () => {
    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    const textarea = await screen.findByPlaceholderText("详细描述问题...");
    const largeFile = new File(["x"], "large.png", { type: "image/png" });
    Object.defineProperty(largeFile, "size", { value: 10 * 1024 * 1024 + 1 });

    fireEvent.paste(textarea, {
      clipboardData: {
        files: [largeFile],
      },
    });

    expect(mocks.pushToast).toHaveBeenCalledWith({ msg: "截图超过 10MB", kind: "error" });
  });

  it("attaches active video workbench diagnostics to new reports", async () => {
    (window as unknown as { __videoWorkbenchDiagnostics?: unknown }).__videoWorkbenchDiagnostics = {
      activeTaskId: "11111111-1111-4111-8111-111111111111",
      byTask: {
        "11111111-1111-4111-8111-111111111111": {
          taskId: "11111111-1111-4111-8111-111111111111",
          frameIndex: 42,
          timelineMode: "selected-track",
          frameClock: { recentSeeks: [{ frameIndex: 42, ms: 18, source: "rvfc" }] },
          framePreview: { cacheHits: 2, cacheMisses: 1 },
        },
      },
    };

    render(<BugReportDrawer open onClose={() => {}} />);

    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "视频 seek 卡顿" },
    });
    fireEvent.change(await screen.findByPlaceholderText("详细描述问题..."), {
      target: { value: "拖动时间轴后首帧很慢" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const payload = mocks.create.mock.calls[0][0];
    expect(payload.task_id).toBe("11111111-1111-4111-8111-111111111111");
    expect(payload.description).toContain("Video Workbench Diagnostics");
    expect(payload.description).toContain('"frameIndex": 42');
    expect(payload.recent_console_errors[0].msg).toBe("[video-workbench-diagnostics]");
    expect(payload.recent_console_errors[0].stack).toContain('"timelineMode": "selected-track"');
  });

  it("attaches bounded Raster Mask compute diagnostics without exposing its task scope", async () => {
    (
      window as unknown as { __rasterMaskComputeDiagnostics?: unknown }
    ).__rasterMaskComputeDiagnostics = {
      activeTaskId: "private-task-scope",
      route: window.location.pathname + window.location.search,
      updatedAt: "2026-07-31T00:00:00.000Z",
      events: [
        {
          recordedAt: "2026-07-31T00:00:00.000Z",
          backend: "cpu",
          cpuStrategy: "packed-separable",
          prepareStrategy: "direct-rle",
          fallbackReason: "navigator-gpu-unavailable",
          failureStage: "adapter-request",
          inputPixels: 4_194_304,
          corePixels: 4_194_304,
          timingsMs: {
            prepare: 1,
            compute: 2,
            uploadSubmit: null,
            readback: null,
            patch: 3,
            total: 6,
          },
          bytes: {
            cpuBudget: 64,
            gpuBudget: 0,
            cpuTransient: 16,
            denseTransient: 0,
            packedIntermediate: 8,
            baseCacheRetained: 0,
            sourceScratchCapacity: 4,
            gpuAllocated: 0,
          },
          cache: { hits: 0, misses: 1, evictions: 0 },
          webGpu: {
            circuitState: "cooldown",
            cooldownRemainingMs: 30_000,
            consecutiveFailures: 1,
            deviceLost: 0,
          },
          pool: { queued: 0, running: 1, sessions: 1, gpuOwnerWorkers: 0 },
        },
      ],
    };

    render(<BugReportDrawer open onClose={() => {}} />);
    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "Mask 膨胀慢" },
    });
    fireEvent.change(await screen.findByPlaceholderText("详细描述问题..."), {
      target: { value: "大图操作回退" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const payload = mocks.create.mock.calls[0][0];
    expect(payload.description).toContain("Raster Mask Compute Diagnostics");
    expect(payload.description).toContain('"cpuStrategy": "packed-separable"');
    expect(payload.description).not.toContain("private-task-scope");
    expect(payload.recent_console_errors[0].msg).toBe("[raster-mask-compute-diagnostics]");
  });

  it("attaches precise-frame diagnostics without leaking sensitive media fields", async () => {
    const taskId = "11111111-1111-4111-8111-111111111111";
    (window as unknown as { __videoWorkbenchDiagnostics?: unknown }).__videoWorkbenchDiagnostics = {
      activeTaskId: taskId,
      byTask: {
        [taskId]: {
          taskId,
          route: window.location.pathname + window.location.search,
          updatedAt: "2026-07-24T00:00:00.000Z",
          preciseFrame: {
            enabled: true,
            supported: true,
            state: "fallback",
            source: "native-bitmap",
            frameIndex: 13,
            chunkId: 1,
            gopStartDecodeIndex: 12,
            targetTimestampUs: 41666,
            codec: "avc1.42E01E",
            fallbackReason: "codec_unsupported",
            lastDemuxMs: 3,
            lastDecodeMs: 7,
            cache: {
              bitmapBytes: 8,
              bitmapBudgetBytes: 256,
              chunkBytes: 4,
              chunkBudgetBytes: 96,
            },
            counters: {
              activeDecoders: 1,
              liveVideoFrames: 0,
              sessionCreates: 2,
              sessionResets: 1,
              encodedChunksSubmitted: 5,
              staleResults: 0,
              prefetchRequests: 0,
              prefetchHits: 0,
            },
          },
        },
      },
    };

    render(<BugReportDrawer open onClose={() => {}} />);
    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "精确帧回退" },
    });
    fireEvent.change(await screen.findByPlaceholderText("详细描述问题..."), {
      target: { value: "暂停后画面是原生 video" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const payload = mocks.create.mock.calls[0][0];
    expect(payload.task_id).toBe(taskId);
    expect(payload.description).toContain("preciseFrame");
    expect(payload.description).toContain('"codec": "avc1.42E01E"');
    expect(payload.description).toContain('"fallbackReason": "codec_unsupported"');
    // 数据最小化:typed snapshot 不可能携带 signed URL / chunk bytes / base64 description。
    expect(payload.description).not.toContain("https://");
    expect(payload.description).not.toContain("base64");
    expect(payload.recent_console_errors[0].msg).toBe("[video-workbench-diagnostics]");
  });

  async function fillNewReport() {
    await screen.findByText("暂无反馈");
    fireEvent.click(screen.getByText("提交新反馈"));
    fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
      target: { value: "Attachment report" },
    });
    const editor = await screen.findByPlaceholderText("详细描述问题...");
    fireEvent.change(editor, { target: { value: "Original draft" } });
    return editor;
  }

  function pasteImages(editor: HTMLElement, count = 1) {
    fireEvent.paste(editor, {
      clipboardData: {
        files: Array.from(
          { length: count },
          (_, index) => new File(["png"], `clip-${index}.png`, { type: "image/png" }),
        ),
      },
    });
  }

  it("retries a partial upload using the original successful server receipt", async () => {
    const receipt = {
      storageKey: "registered-first",
      fileName: "safe-first.png",
      mimeType: "image/png",
      size: 3,
    };
    const secondReceipt = {
      ...receipt,
      storageKey: "registered-second",
      fileName: "safe-second.png",
    };
    mocks.upload
      .mockResolvedValueOnce(receipt)
      .mockRejectedValueOnce(new Error("upload failed"))
      .mockResolvedValueOnce(secondReceipt);
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(<BugReportDrawer open onClose={() => {}} />);
      pasteImages(await fillNewReport(), 2);
      fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));
      await screen.findByText(/截图上传失败/);
      expect(mocks.create).not.toHaveBeenCalled();
      expect(screen.getByPlaceholderText("详细描述问题...")).toHaveValue("Original draft");
      fireEvent.click(screen.getByRole("button", { name: "重试上传" }));
      await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
      expect(mocks.upload).toHaveBeenCalledTimes(3);
      expect(mocks.upload.mock.calls[2][1]).toBe("clip-1.png");
      expect(mocks.create).toHaveBeenCalledWith(
        expect.objectContaining({
          attachments: [receipt, secondReceipt],
          screenshot_url: receipt.storageKey,
        }),
      );
    } finally {
      error.mockRestore();
    }
  });

  it("keeps the five image limit and allows submitting without failed screenshots", async () => {
    mocks.upload.mockRejectedValue(new Error("upload failed"));
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(<BugReportDrawer open onClose={() => {}} />);
      pasteImages(await fillNewReport(), 6);
      expect(screen.getAllByText("移除")).toHaveLength(5);
      expect(mocks.pushToast).toHaveBeenCalledWith({ msg: "最多上传 5 张截图", kind: "error" });
      fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));
      await screen.findByText(/截图上传失败/);
      fireEvent.click(screen.getByRole("button", { name: "跳过截图提交" }));
      await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
      expect(mocks.create).toHaveBeenCalledWith(
        expect.objectContaining({ attachments: [], screenshot_url: null }),
      );
      expect(mocks.upload).toHaveBeenCalledTimes(1);
    } finally {
      error.mockRestore();
    }
  });

  it.each(["account", "same-account-login", "local-token", "close", "unmount"])(
    "does not create a report when %s changes during upload",
    async (change) => {
      let finish!: (value: unknown) => void;
      mocks.upload.mockReturnValue(
        new Promise((resolve) => {
          finish = resolve;
        }),
      );
      const onClose = vi.fn();
      const { rerender, unmount } = render(<BugReportDrawer open onClose={onClose} />);
      pasteImages(await fillNewReport(), 2);
      fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));
      await waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(1));
      act(() => {
        if (change === "account")
          useAuthStore.getState().setAuth("new-token", { id: "new-owner" } as never);
        if (change === "same-account-login") {
          useAuthStore.getState().logout();
          useAuthStore.getState().setAuth("drawer-token", { id: "drawer-owner" } as never);
        }
        if (change === "local-token") localStorage.setItem("token", "new-token");
        if (change === "close") fireEvent.click(screen.getByRole("button", { name: "关闭反馈" }));
        if (change === "unmount") unmount();
      });
      if (change === "close") {
        rerender(<BugReportDrawer open={false} onClose={onClose} />);
        rerender(<BugReportDrawer open onClose={onClose} />);
        fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
          target: { value: "Reopened draft" },
        });
      }
      await act(async () => {
        finish({ storageKey: "late", fileName: "safe.png", mimeType: "image/png", size: 3 });
      });
      expect(mocks.upload).toHaveBeenCalledTimes(1);
      expect(mocks.create).not.toHaveBeenCalled();
      expect(mocks.pushToast).not.toHaveBeenCalledWith(
        expect.objectContaining({ msg: "反馈已提交" }),
      );
      if (change === "close")
        expect(screen.getByPlaceholderText("发生了什么问题？")).toHaveValue("Reopened draft");
    },
  );

  it.each(["account", "close"])(
    "does not clear a new draft or toast after %s changes during create",
    async (change) => {
      let finish!: (value: unknown) => void;
      mocks.create.mockReturnValue(
        new Promise((resolve) => {
          finish = resolve;
        }),
      );
      const onClose = vi.fn();
      const { rerender } = render(<BugReportDrawer open onClose={onClose} />);
      await fillNewReport();
      fireEvent.click(screen.getByRole("button", { name: "提交反馈" }));
      await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
      if (change === "account") {
        act(() => {
          useAuthStore.getState().setAuth("new-token", { id: "new-owner" } as never);
        });
        await fillNewReport();
      } else {
        fireEvent.click(screen.getByRole("button", { name: "关闭反馈" }));
        rerender(<BugReportDrawer open={false} onClose={onClose} />);
        rerender(<BugReportDrawer open onClose={onClose} />);
      }
      fireEvent.change(screen.getByPlaceholderText("发生了什么问题？"), {
        target: { value: "New draft" },
      });
      await act(async () => {
        finish({});
      });
      expect(screen.getByPlaceholderText("发生了什么问题？")).toHaveValue("New draft");
      expect(mocks.pushToast).not.toHaveBeenCalledWith(
        expect.objectContaining({ msg: "反馈已提交" }),
      );
    },
  );

  it("shows attachment download progress, a retryable error, and saves the retried blob", async () => {
    const attachment = {
      storageKey: "owned-image",
      fileName: "screenshot.png",
      mimeType: "image/png",
      size: 2048,
    };
    mocks.get.mockResolvedValue({
      id: "report",
      display_id: "B-1",
      title: "With image",
      description: "Description",
      status: "new",
      attachments: [attachment],
      comments: [],
    });
    let fail!: (reason: unknown) => void;
    mocks.download
      .mockReturnValueOnce(
        new Promise((_, reject) => {
          fail = reject;
        }),
      )
      .mockResolvedValueOnce(new Blob(["png"]));
    const createUrl = vi.fn().mockReturnValue("blob:drawer-image");
    const revokeUrl = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(class extends URL {}, {
        createObjectURL: createUrl,
        revokeObjectURL: revokeUrl,
      }),
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    try {
      render(<BugReportDrawer open focusBugId="report" onClose={() => {}} />);
      const button = await screen.findByRole("button", { name: /screenshot.png/ });
      fireEvent.click(button);
      expect(button).toBeDisabled();
      expect(screen.getByText("下载中…")).toBeInTheDocument();
      await act(async () => {
        fail({ status: 410 });
      });
      expect(await screen.findByRole("alert")).toHaveTextContent("附件已过期");
      fireEvent.click(screen.getByRole("button", { name: /screenshot.png/ }));
      await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
      expect(mocks.download).toHaveBeenCalledWith("report", "owned-image", expect.any(AbortSignal));
      expect(revokeUrl).toHaveBeenCalledWith("blob:drawer-image");
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      click.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
