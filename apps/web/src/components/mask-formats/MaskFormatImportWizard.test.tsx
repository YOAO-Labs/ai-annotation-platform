import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MaskFormatImportWizard } from "./MaskFormatImportWizard";

vi.mock("@/api/maskFormats", () => ({
  maskFormatsApi: {
    list: vi.fn(),
    initImportUpload: vi.fn(),
    preflightImport: vi.fn(),
    executeImport: vi.fn(),
  },
}));

import { maskFormatsApi } from "@/api/maskFormats";

function plan(lossClass: "lossless" | "lossy" | "unsupported", unknownLabels: string[] = []) {
  return {
    import_id: "import-1",
    receipt: "mfi_receipt_123456789",
    receipt_expires_at: "2026-07-23T12:00:00Z",
    plan: {
      format_id: "coco",
      direction: "import" as const,
      adapter_version: "2.0.0",
      manifest_version: "1",
      media_type: "image",
      loss_class: lossClass,
      staged_object_key: "mask-formats/key/file.json",
      staged_sha256: "a".repeat(64),
      mapping_digest: "b".repeat(64),
      options_digest: "c".repeat(64),
      items: [],
      unknown_labels: unknownLabels,
      size_conflicts: [],
      overlap_conflicts: [],
      id_mapping: {},
      frame_mapping: {},
      estimated_objects: 2,
      estimated_files: 1,
      estimated_bytes: 128,
      losses: [],
      skips: unknownLabels.map((label) => ({ code: "unknown_label", message: label, detail: {} })),
      warnings: [],
      plan_digest: "d".repeat(64),
    },
  };
}

const uploadUrl = "https://storage.invalid/upload";
const upload = vi.fn();

describe("MaskFormatImportWizard", () => {
  beforeEach(() => {
    upload.mockClear();
    Object.defineProperty(File.prototype, "arrayBuffer", {
      configurable: true,
      value: vi.fn(async () => new Uint8Array([1, 2, 3]).buffer),
    });
    vi.stubGlobal("crypto", {
      subtle: {
        digest: vi.fn(async () => new Uint8Array(32).fill(10).buffer),
      },
    });
    vi.mocked(maskFormatsApi.list).mockResolvedValue([
      {
        format_id: "coco",
        label: "COCO Instance",
        adapter_version: "2.0.0",
        manifest_version: "1",
        media_types: ["image"],
        import_capability: { supported: true, verified: true, enabled_for_ui: true },
        export_capability: { supported: true, verified: true, enabled_for_ui: true },
        option_schema: {},
      },
      {
        format_id: "hidden",
        label: "未验证格式",
        adapter_version: "1.0.0",
        manifest_version: "1",
        media_types: ["image"],
        import_capability: { supported: true, verified: false, enabled_for_ui: false },
        export_capability: { supported: false, verified: false, enabled_for_ui: false },
        option_schema: {},
      },
    ]);
    vi.mocked(maskFormatsApi.initImportUpload).mockResolvedValue({
      object_key: "mask-formats/key/file.json",
      upload_url: uploadUrl,
      expires_in: 900,
    });
    vi.mocked(maskFormatsApi.executeImport).mockResolvedValue({
      id: "import-1",
      project_id: "project-1",
      async_job_id: "job-1",
      format_id: "coco",
      status: "pending",
      result: {},
    });
    const interceptedFetch = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input !== uploadUrl || init?.method !== "PUT") return interceptedFetch(input, init);
      upload(new Headers(init.headers).get("content-type"));
      return new Response(null, { status: 204 });
    });
  });

  it("只展示 registry 已验证格式，并按 staged receipt 发起导入", async () => {
    vi.mocked(maskFormatsApi.preflightImport).mockResolvedValue(plan("lossless"));
    const onQueued = vi.fn();
    render(
      <MaskFormatImportWizard open projectId="project-1" onClose={() => {}} onQueued={onQueued} />,
    );

    expect(await screen.findByRole("option", { name: "COCO Instance" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "未验证格式" })).not.toBeInTheDocument();
    const input = screen.getByLabelText("文件") as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["{}"], "coco.json", { type: "application/json" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    expect(await screen.findByText("无损，可执行")).toBeInTheDocument();
    expect(upload.mock.calls).toEqual([["application/json"]]);
    expect(maskFormatsApi.preflightImport).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        format_id: "coco",
        staged_object_key: "mask-formats/key/file.json",
        staged_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认导入" }));
    await waitFor(() =>
      expect(maskFormatsApi.executeImport).toHaveBeenCalledWith(
        "project-1",
        "mfi_receipt_123456789",
        "d".repeat(64),
        false,
      ),
    );
    expect(onQueued).toHaveBeenCalledOnce();
  });

  it("未知类别必须映射并重新预检", async () => {
    vi.mocked(maskFormatsApi.preflightImport)
      .mockResolvedValueOnce(plan("unsupported", ["vehicle"]))
      .mockResolvedValueOnce(plan("lossless"));
    render(<MaskFormatImportWizard open projectId="project-1" onClose={() => {}} />);
    await screen.findByRole("option", { name: "COCO Instance" });
    fireEvent.change(screen.getByLabelText("文件"), {
      target: { files: [new File(["{}"], "coco.json", { type: "application/json" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    const mapping = await screen.findByPlaceholderText("项目类别名");
    fireEvent.change(mapping, { target: { value: "car" } });
    fireEvent.click(screen.getByRole("button", { name: "应用映射并重新预检" }));
    await waitFor(() =>
      expect(maskFormatsApi.preflightImport).toHaveBeenLastCalledWith(
        "project-1",
        expect.objectContaining({ mapping: { labels: { vehicle: "car" } } }),
      ),
    );
    expect(await screen.findByText("无损，可执行")).toBeInTheDocument();
  });

  it("有损导入必须显式确认", async () => {
    vi.mocked(maskFormatsApi.preflightImport).mockResolvedValue(plan("lossy"));
    render(<MaskFormatImportWizard open projectId="project-1" onClose={() => {}} />);
    await screen.findByRole("option", { name: "COCO Instance" });
    fireEvent.change(screen.getByLabelText("文件"), {
      target: {
        files: [new File(["{}"], "mask.json", { type: "application/json" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));

    expect(await screen.findByText("有损，需确认")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "确认导入" });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText("我已了解上述像素或结构损失"));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() =>
      expect(maskFormatsApi.executeImport).toHaveBeenCalledWith(
        "project-1",
        "mfi_receipt_123456789",
        "d".repeat(64),
        true,
      ),
    );
  });

  it("视频格式把 sparse gap 与 MOTS frame base 绑定到预检", async () => {
    vi.mocked(maskFormatsApi.list).mockResolvedValue([
      {
        format_id: "coco-frames-seg",
        label: "COCO Frames Segmentation",
        adapter_version: "2.0.0",
        manifest_version: "1",
        media_types: ["video"],
        import_capability: { supported: true, verified: true, enabled_for_ui: true },
        export_capability: { supported: true, verified: true, enabled_for_ui: true },
        option_schema: { frame_base: { type: "integer", enum: [0, 1], default: 1 } },
      },
      {
        format_id: "youtube-vos",
        label: "YouTube-VOS",
        adapter_version: "1.0.0",
        manifest_version: "1",
        media_types: ["video"],
        import_capability: { supported: true, verified: true, enabled_for_ui: true },
        export_capability: { supported: true, verified: true, enabled_for_ui: true },
        option_schema: {},
      },
      {
        format_id: "mots",
        label: "MOTS",
        adapter_version: "1.0.0",
        manifest_version: "1",
        media_types: ["video"],
        import_capability: { supported: true, verified: true, enabled_for_ui: true },
        export_capability: { supported: true, verified: true, enabled_for_ui: true },
        option_schema: {},
      },
    ]);
    vi.mocked(maskFormatsApi.preflightImport).mockResolvedValue(plan("lossy"));
    render(<MaskFormatImportWizard open projectId="video-project" onClose={() => {}} />);

    await screen.findByRole("option", { name: "YouTube-VOS" });
    fireEvent.change(screen.getByLabelText("格式"), { target: { value: "youtube-vos" } });
    fireEvent.change(screen.getByLabelText("稀疏帧策略"), {
      target: { value: "nearest_hold" },
    });
    fireEvent.change(screen.getByLabelText("文件"), {
      target: { files: [new File(["zip"], "ytvos.zip", { type: "application/zip" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await waitFor(() =>
      expect(maskFormatsApi.preflightImport).toHaveBeenLastCalledWith(
        "video-project",
        expect.objectContaining({
          format_id: "youtube-vos",
          options: { overwrite: false, sparse_gap_policy: "nearest_hold" },
        }),
      ),
    );

    fireEvent.change(screen.getByLabelText("格式"), { target: { value: "coco-frames-seg" } });
    expect(screen.getByLabelText("帧号基准")).toHaveValue("1");
    fireEvent.change(screen.getByLabelText("文件"), {
      target: { files: [new File(["{}"], "coco-frames.json", { type: "application/json" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await waitFor(() =>
      expect(maskFormatsApi.preflightImport).toHaveBeenLastCalledWith(
        "video-project",
        expect.objectContaining({
          format_id: "coco-frames-seg",
          options: { overwrite: false, frame_base: 1 },
        }),
      ),
    );

    fireEvent.change(screen.getByLabelText("格式"), { target: { value: "mots" } });
    fireEvent.change(screen.getByLabelText("帧号基准"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("文件"), {
      target: { files: [new File(["zip"], "mots.zip", { type: "application/zip" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await waitFor(() =>
      expect(maskFormatsApi.preflightImport).toHaveBeenLastCalledWith(
        "video-project",
        expect.objectContaining({
          format_id: "mots",
          options: { overwrite: false, frame_base: 1 },
        }),
      ),
    );
  });
});
