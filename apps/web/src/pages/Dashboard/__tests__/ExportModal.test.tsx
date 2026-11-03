/**
 * ExportModal 关键交互单测（前身 ExportSection.test.tsx）。
 * v0.10.43 · 多目标导出：targets 数组入参 + 多选切换。
 * B-47 后导出入口收进 ⋮ 菜单，独立包装器 ExportSection 已删除；
 * 表单逻辑（目标多选 / 属性开关 / 视频帧模式）改为直接渲染 ExportModal 验证。
 *
 * 覆盖：属性开关、目标多选和视频帧模式透传。
 * 用 vi.mock 拦截 api 模块，断言入参。
 */
import { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ExportModal } from "../ExportModal";

vi.mock("@/api/projects", () => ({
  projectsApi: {
    exportProject: vi.fn(async () => ({ job_id: "j1" })),
    preflightLidarExport: vi.fn(),
  },
}));
vi.mock("@/api/maskFormats", () => ({
  maskFormatsApi: {
    preflightExport: vi.fn(),
  },
}));
vi.mock("@/api/tasks", () => ({
  tasksApi: {
    listByProject: vi.fn(),
  },
}));
vi.mock("@/api/videoTracker", () => ({
  videoTrackerApi: {
    segments: vi.fn(),
  },
}));

import { projectsApi } from "@/api/projects";
import { maskFormatsApi } from "@/api/maskFormats";
import { tasksApi } from "@/api/tasks";
import { videoTrackerApi } from "@/api/videoTracker";

function preflight(
  lossClass: "lossless" | "lossy" | "unsupported" = "lossless",
  lossCode?: string,
) {
  const losses = lossCode ? [{ code: lossCode, message: "格式损失说明", detail: {} }] : [];
  return {
    plans: [
      {
        format_id: "coco",
        direction: "export" as const,
        adapter_version: "1.0.0",
        manifest_version: "1",
        media_type: "image",
        loss_class: lossClass,
        staged_object_key: null,
        staged_sha256: null,
        mapping_digest: "a".repeat(64),
        options_digest: "b".repeat(64),
        items: [],
        unknown_labels: [],
        size_conflicts: [],
        overlap_conflicts: [],
        id_mapping: {},
        frame_mapping: {},
        estimated_objects: 2,
        estimated_files: 1,
        estimated_bytes: 128,
        losses,
        skips: [],
        warnings: [],
        plan_digest: "c".repeat(64),
      },
    ],
    loss_class: lossClass,
    estimated_objects: 2,
    estimated_files: 1,
    estimated_bytes: 128,
    losses,
    warnings: [],
    preflight_digest: "d".repeat(64),
  };
}

function lidarPreflight(
  ready: boolean,
  selectedCameraRole: string | null = null,
  issues: Array<{ code: string; message: string }> = [],
) {
  return {
    ready,
    camera_roles: ["camera_front", "camera_back"],
    selected_camera_role: selectedCameraRole,
    checked_tasks: 39,
    issue_count: issues.length,
    issues_truncated: false,
    issues: issues.map((issue) => ({
      ...issue,
      task_id: null,
      task_display_id: null,
      frame_key: null,
      camera_role: selectedCameraRole,
    })),
  };
}

// 受控 harness：导出完成回调把 open 置 false，便于断言弹窗关闭。
function ExportModalHarness({
  projectId,
  projectTypeKey,
}: {
  projectId: string;
  projectTypeKey?: string;
}) {
  const [open, setOpen] = useState(true);
  return (
    <ExportModal
      open={open}
      onClose={() => setOpen(false)}
      projectId={projectId}
      projectTypeKey={projectTypeKey}
    />
  );
}

function submitExport() {
  fireEvent.click(screen.getByRole("button", { name: "开始导出" }));
}

describe("ExportModal", () => {
  beforeEach(() => {
    vi.mocked(projectsApi.exportProject).mockClear();
    vi.mocked(projectsApi.preflightLidarExport).mockReset();
    vi.mocked(projectsApi.preflightLidarExport).mockImplementation(
      async (_projectId, targets, lidar) =>
        targets.includes("kitti") && !lidar?.kittiCameraRole
          ? lidarPreflight(false, null, [
              { code: "kitti_camera_required", message: "KITTI 导出必须显式选择相机" },
            ])
          : lidarPreflight(true, lidar?.kittiCameraRole ?? null),
    );
    vi.mocked(maskFormatsApi.preflightExport).mockReset();
    vi.mocked(maskFormatsApi.preflightExport).mockResolvedValue(preflight());
    vi.mocked(tasksApi.listByProject).mockReset();
    vi.mocked(videoTrackerApi.segments).mockReset();
  });

  it("勾掉 includeAttributes → 入参 false", async () => {
    render(<ExportModalHarness projectId="p1" />);
    const cb = screen.getByLabelText("包含属性数据") as HTMLInputElement;
    expect(cb.checked).toBe(true);
    fireEvent.click(cb);
    expect(cb.checked).toBe(false);
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p1", ["coco"], {
      includeAttributes: false,
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("多选 coco + 展开 YOLO 选分割 → targets 含两者", async () => {
    render(<ExportModalHarness projectId="p2" />);
    fireEvent.click(screen.getByText("YOLO")); // 展开分组
    fireEvent.click(screen.getByText("分割"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p2", ["coco", "yolo-seg"], {
      includeAttributes: true,
    });
  });

  it("取消默认 coco 改选 YOLO 旋转框 → targets=[yolo-obb]", async () => {
    render(<ExportModalHarness projectId="p2b" />);
    fireEvent.click(screen.getByText("COCO")); // 取消默认 coco
    fireEvent.click(screen.getByText("YOLO")); // 展开分组
    fireEvent.click(screen.getByText("旋转框"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p2b", ["yolo-obb"], {
      includeAttributes: true,
    });
  });

  it("Indexed PNG 显式重叠策略同时传入预检和导出", async () => {
    render(<ExportModalHarness projectId="p-indexed" />);
    fireEvent.click(screen.getByText("COCO"));
    fireEvent.click(screen.getByText("Indexed PNG"));
    fireEvent.change(screen.getByLabelText("Indexed PNG 重叠策略"), {
      target: { value: "z_order" },
    });
    submitExport();

    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(maskFormatsApi.preflightExport).toHaveBeenCalledWith("p-indexed", ["indexed-png"], {
      includeAttributes: true,
      indexedOverlapPolicy: "z_order",
    });
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p-indexed", ["indexed-png"], {
      includeAttributes: true,
      indexedOverlapPolicy: "z_order",
    });
  });

  it("视频项目展示视频目标并传递 Video JSON frame mode", async () => {
    render(<ExportModalHarness projectId="p3" projectTypeKey="video-track" />);

    expect(screen.getByText("Video JSON")).toBeInTheDocument();
    expect(screen.getByText("YOLO 逐帧")).toBeInTheDocument();
    expect(screen.getByText("MOT")).toBeInTheDocument();
    expect(screen.getByText("KITTI")).toBeInTheDocument();
    expect(screen.getByText("YouTube-VOS")).toBeInTheDocument();
    expect(screen.getByText("MOTS")).toBeInTheDocument();
    expect(screen.queryByText("YOLO")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("所有帧"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p3", ["video_json"], {
      includeAttributes: true,
      videoFrameMode: "all_frames",
    });
  });

  it("视频帧范围同时进入预检和导出", async () => {
    vi.mocked(tasksApi.listByProject).mockResolvedValue({
      items: [{ id: "task-1", display_id: "T-1", file_name: "clip.mp4" }] as never,
      total: 1,
      limit: 100,
      offset: 0,
      next_cursor: null,
    });
    vi.mocked(videoTrackerApi.segments).mockResolvedValue({
      task_id: "task-1",
      dataset_item_id: "item-1",
      segment_size_frames: 5,
      collaboration_enabled: false,
      overlap_frames: 0,
      segments: [
        {
          id: "seg-1",
          start_frame: 0,
          end_frame: 4,
          work_start_frame: 0,
          work_end_frame: 4,
          segment_index: 0,
          status: "open",
        },
        {
          id: "seg-2",
          start_frame: 5,
          end_frame: 9,
          work_start_frame: 5,
          work_end_frame: 9,
          segment_index: 1,
          status: "open",
        },
      ],
    });
    render(<ExportModalHarness projectId="p-range" projectTypeKey="video-track" />);

    fireEvent.change(screen.getByLabelText("导出范围"), { target: { value: "task" } });
    await screen.findByRole("option", { name: "T-1 · clip.mp4" });
    await waitFor(() =>
      expect((screen.getByLabelText("结束 Segment") as HTMLSelectElement).options).toHaveLength(2),
    );
    fireEvent.change(screen.getByLabelText("范围方式"), { target: { value: "frames" } });
    fireEvent.change(screen.getByLabelText("起始帧"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("结束帧"), { target: { value: "7" } });
    submitExport();

    const options = {
      includeAttributes: true,
      videoFrameMode: "keyframes" as const,
      scope: {
        task_id: "task-1",
        selection: { kind: "frames" as const, from_frame: 2, to_frame: 7 },
      },
    };
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(maskFormatsApi.preflightExport).toHaveBeenCalledWith("p-range", ["video_json"], options);
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p-range", ["video_json"], options);
  });

  it("DAVIS / YouTube-VOS overlap 与 MOTS frame base 同时进入预检和导出", async () => {
    render(<ExportModalHarness projectId="p-video-mask" projectTypeKey="video-track" />);
    fireEvent.click(screen.getByText("Video JSON"));
    fireEvent.click(screen.getByText("DAVIS Mask"));
    fireEvent.click(screen.getByText("YouTube-VOS"));
    fireEvent.click(screen.getByText("MOTS"));
    fireEvent.change(screen.getByLabelText("视频 Mask 重叠策略"), {
      target: { value: "z_order" },
    });
    fireEvent.change(screen.getByLabelText("MOTS 帧号基准"), {
      target: { value: "1" },
    });
    submitExport();

    const options = {
      includeAttributes: true,
      videoOverlapPolicy: "z_order" as const,
      motsFrameBase: 1 as const,
    };
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(maskFormatsApi.preflightExport).toHaveBeenCalledWith(
      "p-video-mask",
      ["davis", "youtube-vos", "mots"],
      options,
    );
    expect(projectsApi.exportProject).toHaveBeenCalledWith(
      "p-video-mask",
      ["davis", "youtube-vos", "mots"],
      options,
    );
  });

  it("视频项目可单独导出 YOLO 逐帧检测集", async () => {
    render(<ExportModalHarness projectId="p4" projectTypeKey="video-track" />);

    fireEvent.click(screen.getByText("Video JSON"));
    fireEvent.click(screen.getByText("YOLO 逐帧"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p4", ["yolo-frames-det"], {
      includeAttributes: true,
    });
  });

  it("视频项目可单独导出 YOLO 逐帧分割集", async () => {
    render(<ExportModalHarness projectId="p4b" projectTypeKey="video-track" />);

    expect(screen.getByText("YOLO 逐帧分割")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Video JSON")); // 取消默认 video_json
    fireEvent.click(screen.getByText("YOLO 逐帧分割"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p4b", ["yolo-frames-seg"], {
      includeAttributes: true,
    });
  });

  it("视频项目可同选 YOLO 逐帧检测 + 分割，互不覆盖", async () => {
    render(<ExportModalHarness projectId="p4c" projectTypeKey="video-track" />);

    fireEvent.click(screen.getByText("Video JSON")); // 取消默认 video_json
    fireEvent.click(screen.getByText("YOLO 逐帧"));
    fireEvent.click(screen.getByText("YOLO 逐帧分割"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith(
      "p4c",
      ["yolo-frames-det", "yolo-frames-seg"],
      { includeAttributes: true },
    );
  });

  it("视频项目可单独导出 COCO 逐帧分割集", async () => {
    render(<ExportModalHarness projectId="p6" projectTypeKey="video-track" />);

    expect(screen.getByText("COCO 逐帧分割")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Video JSON")); // 取消默认 video_json
    fireEvent.click(screen.getByText("COCO 逐帧分割"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p6", ["coco-frames-seg"], {
      includeAttributes: true,
    });
  });

  it("视频项目可同选 YOLO 逐帧分割 + COCO 逐帧分割，互不覆盖", async () => {
    render(<ExportModalHarness projectId="p7" projectTypeKey="video-track" />);

    fireEvent.click(screen.getByText("Video JSON")); // 取消默认 video_json
    fireEvent.click(screen.getByText("YOLO 逐帧分割"));
    fireEvent.click(screen.getByText("COCO 逐帧分割"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith(
      "p7",
      ["yolo-frames-seg", "coco-frames-seg"],
      { includeAttributes: true },
    );
  });

  it("点云项目展示标准 3D 目标并默认导出 原生 JSON", async () => {
    render(<ExportModalHarness projectId="p5" projectTypeKey="lidar" />);

    expect(screen.getByText("KITTI 3D")).toBeInTheDocument();
    expect(screen.getByText("Multi-camera COCO")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^nuScenes/ })).toBeEnabled();
    expect(screen.getByText("Point Mask")).toBeInTheDocument();
    expect(screen.queryByText("COCO")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("KITTI 3D"));
    await screen.findByRole("option", { name: "front" });
    fireEvent.change(screen.getByLabelText("KITTI 投影相机"), {
      target: { value: "camera_front" },
    });
    await screen.findByText("预检通过 · 39 帧");
    fireEvent.click(screen.getByText("Point Mask"));
    await waitFor(() => expect(screen.getByRole("button", { name: "开始导出" })).toBeEnabled());
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.exportProject).toHaveBeenCalledWith(
      "p5",
      ["aap_json", "kitti", "pointmask"],
      {
        includeAttributes: true,
        lidar: { kittiCameraRole: "camera_front" },
      },
    );
    expect(maskFormatsApi.preflightExport).not.toHaveBeenCalled();
  });

  it("nuScenes 单独选中时自动预检并可入队", async () => {
    render(<ExportModalHarness projectId="p-nuscenes" projectTypeKey="lidar" />);

    fireEvent.click(screen.getByText("原生 JSON"));
    fireEvent.click(screen.getByText("nuScenes"));
    await screen.findByText("预检通过 · 39 帧");
    expect(screen.queryByLabelText("KITTI 投影相机")).not.toBeInTheDocument();
    submitExport();

    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.preflightLidarExport).toHaveBeenCalledWith(
      "p-nuscenes",
      ["nuscenes"],
      undefined,
    );
    expect(projectsApi.exportProject).toHaveBeenCalledWith("p-nuscenes", ["nuscenes"], {
      includeAttributes: true,
    });
  });

  it("多相机 COCO 单独选中时自动预检且不要求 KITTI 相机", async () => {
    render(<ExportModalHarness projectId="p-multicamera-coco" projectTypeKey="lidar" />);

    fireEvent.click(screen.getByText("原生 JSON"));
    fireEvent.click(screen.getByText("Multi-camera COCO"));
    await screen.findByText("预检通过 · 39 帧");
    expect(screen.queryByLabelText("KITTI 投影相机")).not.toBeInTheDocument();
    submitExport();

    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalled());
    expect(projectsApi.preflightLidarExport).toHaveBeenCalledWith(
      "p-multicamera-coco",
      ["coco-multicamera"],
      undefined,
    );
    expect(projectsApi.exportProject).toHaveBeenCalledWith(
      "p-multicamera-coco",
      ["coco-multicamera"],
      { includeAttributes: true },
    );
  });

  it("点云 KITTI 预检问题会定位展示并阻止入队", async () => {
    vi.mocked(projectsApi.preflightLidarExport).mockResolvedValue(
      lidarPreflight(false, "camera_front", [
        { code: "camera_calibration_invalid", message: "T-1059 缺少有效内外参" },
      ]),
    );
    render(<ExportModalHarness projectId="p-lidar-blocked" projectTypeKey="lidar" />);

    fireEvent.click(screen.getByText("KITTI 3D"));
    await screen.findByRole("option", { name: "front" });
    fireEvent.change(screen.getByLabelText("KITTI 投影相机"), {
      target: { value: "camera_front" },
    });

    await screen.findByText("camera_calibration_invalid");
    expect(screen.getByRole("button", { name: "开始导出" })).toBeDisabled();
    expect(projectsApi.exportProject).not.toHaveBeenCalled();
  });

  it("有损预检显示稳定 code，确认前不入队", async () => {
    vi.mocked(maskFormatsApi.preflightExport).mockResolvedValue(
      preflight("lossy", "unknown_future_loss"),
    );
    render(<ExportModalHarness projectId="p-lossy" />);

    submitExport();
    await screen.findByTestId("mask-format-preflight");
    expect(screen.getByText("unknown_future_loss")).toBeInTheDocument();
    expect(projectsApi.exportProject).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText("我已了解以上格式损失，继续导出"));
    submitExport();
    await waitFor(() => expect(projectsApi.exportProject).toHaveBeenCalledOnce());
  });

  it("unsupported 预检阻止导出", async () => {
    vi.mocked(maskFormatsApi.preflightExport).mockResolvedValue(preflight("unsupported"));
    render(<ExportModalHarness projectId="p-unsupported" />);

    submitExport();
    await screen.findByText("预检阻止 · 不支持");
    expect(projectsApi.exportProject).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "开始导出" })).toBeDisabled();
  });
});
