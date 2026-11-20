/**
 * VideoSamplingSection 单测 — 项目级视频帧采样配置.
 *
 * 覆盖: 初值加载 / mode 切换即时保存 / fps & step 失焦保存 / 预览文案 /
 *       非法态不提交.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const mockUpdateMutate = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useUpdateProject: () => ({ mutate: mockUpdateMutate, isPending: false }),
}));
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { VideoSamplingSection } from "./VideoSamplingSection";
import type { ProjectResponse, VideoSamplingConfig } from "@/api/projects";

function makeProject(
  video_sampling?: VideoSamplingConfig | null,
  collaboration?: ProjectResponse["video_collaboration"],
): ProjectResponse {
  return {
    id: "p1",
    display_id: "P-1",
    name: "Demo Video",
    type_key: "video-det",
    type_label: "视频检测",
    data_type: "video",
    status: "in_progress",
    classes: [],
    video_sampling: video_sampling ?? null,
    video_collaboration: collaboration ?? null,
  } as unknown as ProjectResponse;
}

describe("VideoSamplingSection", () => {
  beforeEach(() => {
    mockUpdateMutate.mockReset();
    mockPushToast.mockReset();
  });

  it("默认无配置 → mode=none, 预览显示不采样, 无交互不自动提交", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    expect(screen.getByTestId("video-sampling-preview").textContent).toMatch(/不采样/);
    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });

  it("加载已有 fps 配置作为初值", () => {
    render(<VideoSamplingSection project={makeProject({ mode: "fps", target_fps: 10 })} />);
    expect(screen.getByDisplayValue("10")).toBeInTheDocument();
    expect(screen.getByTestId("video-sampling-preview").textContent).toMatch(/标注 10 fps/);
  });

  it("切到 fps 模式 target 为空 → 自动填默认 10 并提交（避免 UI/DB 不一致）", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按目标 fps"));
    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_sampling: { mode: "fps", target_fps: 10 } },
      expect.any(Object),
    );
  });

  it("fps 模式填合法 target 失焦 → 提交 { mode: fps, target_fps }", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按目标 fps"));
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "12" } });
    expect(screen.getByTestId("video-sampling-preview").textContent).toMatch(/标注 12 fps/);
    fireEvent.blur(input);
    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_sampling: { mode: "fps", target_fps: 12 } },
      expect.any(Object),
    );
  });

  it("step 模式填合法整数 失焦 → 提交 { mode: step, frame_step }, 预览含 step", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按帧间隔"));
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "5" } });
    expect(screen.getByTestId("video-sampling-preview").textContent).toMatch(/每 5 帧打点/);
    fireEvent.blur(input);
    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_sampling: { mode: "step", frame_step: 5 } },
      expect.any(Object),
    );
  });

  it("切到 step 模式 frame_step 为空 → 自动填默认 1 并提交", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按帧间隔"));
    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_sampling: { mode: "step", frame_step: 1 } },
      expect.any(Object),
    );
  });

  it("step 模式改成非法值 0（<1）失焦 → 不再追加提交（保留切换时的默认）", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按帧间隔"));
    expect(mockUpdateMutate).toHaveBeenCalledTimes(1); // 切换时的默认提交
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.blur(input);
    expect(mockUpdateMutate).toHaveBeenCalledTimes(1); // 非法 blur 不再提交
  });

  it("从 fps 切回不采样 → 即时提交 { mode: none }", () => {
    render(<VideoSamplingSection project={makeProject()} />);
    fireEvent.click(screen.getByLabelText("按目标 fps"));
    fireEvent.click(screen.getByLabelText("不采样（所有帧）"));
    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_sampling: { mode: "none" } },
      expect.any(Object),
    );
  });

  it("启用分段协同时提交 overlap 配置", () => {
    render(<VideoSamplingSection project={makeProject()} />);

    fireEvent.click(screen.getByLabelText("启用 segment overlap 协同"));

    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_collaboration: { enabled: true, overlap_frames: 1 } },
      expect.any(Object),
    );
  });

  it("修改已启用协同的 overlap 帧数后失焦保存", () => {
    render(
      <VideoSamplingSection project={makeProject(null, { enabled: true, overlap_frames: 10 })} />,
    );
    const input = screen.getByLabelText("相邻重叠帧数");

    fireEvent.change(input, { target: { value: "24" } });
    fireEvent.blur(input);

    expect(mockUpdateMutate).toHaveBeenLastCalledWith(
      { video_collaboration: { enabled: true, overlap_frames: 24 } },
      expect.any(Object),
    );
  });
});
