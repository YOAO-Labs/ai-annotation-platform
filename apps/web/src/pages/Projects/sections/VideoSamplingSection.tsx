// v0.10.29 · 项目级视频帧采样配置（逻辑采样，见
// docs/plans/archive/2026-05-21-v0.10.29-video-frame-sampling.md §2/§4 Wave2-D）。
//
// 仅在 data_type === "video" 的项目设置页渲染。
// mode=none 不采样；mode=fps 按目标 fps；mode=step 按帧间隔。
// 保存走 useUpdateProject (PATCH)，payload { video_sampling: {...} }。
//
// 结构/样式/保存交互仿 RenderingConfigSection.tsx。

import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { useToastStore } from "@/components/ui/Toast";
import { useUpdateProject } from "@/hooks/useProjects";
import type { ProjectResponse, VideoSamplingConfig } from "@/api/projects";
import type { VideoCollaborationConfig } from "@/api/generated/types.gen";
import { LABEL_CLASS } from "./formClasses";

const ROW_CLASS = "flex flex-col gap-1.5 py-2.5";
const INPUT_CLASS =
  "w-full appearance-none rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground outline-none";

type SamplingMode = "none" | "fps" | "step";

interface DraftState {
  mode: SamplingMode;
  /** fps 模式的目标帧率原始输入（保留字符串以便编辑） */
  targetFps: string;
  /** step 模式的帧间隔原始输入 */
  frameStep: string;
}

function initDraft(cfg: VideoSamplingConfig | null | undefined): DraftState {
  const mode = (cfg?.mode ?? "none") as SamplingMode;
  return {
    mode,
    targetFps: cfg?.target_fps != null ? String(cfg.target_fps) : "",
    frameStep: cfg?.frame_step != null ? String(cfg.frame_step) : "",
  };
}

/** 把草稿状态转成后端 payload；非法时返回 null。 */
function buildConfig(draft: DraftState): VideoSamplingConfig | null {
  if (draft.mode === "none") return { mode: "none" };
  if (draft.mode === "fps") {
    const fps = Number(draft.targetFps);
    if (!Number.isFinite(fps) || fps <= 0) return null;
    return { mode: "fps", target_fps: fps };
  }
  // step
  const step = Number(draft.frameStep);
  if (!Number.isInteger(step) || step < 1) return null;
  return { mode: "step", frame_step: step };
}

/** 预览文案：源 fps 在设置页拿不到，故只回显逻辑关系。 */
function previewText(draft: DraftState): string {
  if (draft.mode === "none") {
    return "不采样：所有源帧都是导航网格点（每 1 帧打点）。";
  }
  if (draft.mode === "fps") {
    const fps = Number(draft.targetFps);
    if (!Number.isFinite(fps) || fps <= 0) return "请填写大于 0 的目标 fps。";
    return `源视频 按视频实际帧率 → 标注 ${fps} fps（每隔约 round(源fps / ${fps}) 帧打点）。`;
  }
  const step = Number(draft.frameStep);
  if (!Number.isInteger(step) || step < 1) return "请填写 ≥1 的整数帧间隔。";
  return `源视频 按视频实际帧率 → 每 ${step} 帧打点（step=${step}）。`;
}

export function VideoSamplingSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const update = useUpdateProject(project.id);
  const [draft, setDraft] = useState<DraftState>(() => initDraft(project.video_sampling));
  const [collaboration, setCollaboration] = useState<VideoCollaborationConfig>(
    project.video_collaboration ?? { enabled: false, overlap_frames: 0 },
  );

  const config = buildConfig(draft);
  const valid = config !== null;

  // 自动保存：切换采样方式即时存；数字输入失焦存。非法配置不提交，等用户
  // 填到合法再触发。失败弹 toast，成功静默。
  const commit = (nextDraft: DraftState) => {
    const cfg = buildConfig(nextDraft);
    if (!cfg) return;
    update.mutate(
      { video_sampling: cfg },
      { onError: () => pushToast({ msg: "保存失败", kind: "warning" }) },
    );
  };

  const setMode = (mode: SamplingMode) => {
    const next = { ...draft, mode };
    // 切到 fps/step 时若对应输入为空，填一个合法默认值，保证 commit 一定落库，
    // 避免 UI radio 已切到新模式但后端仍是旧模式（刷新即回滚）。
    if (mode === "fps" && !next.targetFps.trim()) next.targetFps = "10";
    if (mode === "step" && !next.frameStep.trim()) next.frameStep = "1";
    setDraft(next);
    commit(next);
  };

  const commitCollaboration = (next: VideoCollaborationConfig) => {
    setCollaboration(next);
    update.mutate(
      { video_collaboration: next },
      {
        onError: (error) => {
          setCollaboration(project.video_collaboration ?? { enabled: false, overlap_frames: 0 });
          pushToast({ msg: "协同配置保存失败", sub: (error as Error).message, kind: "warning" });
        },
      },
    );
  };

  return (
    <Card>
      <div className="border-b border-border px-4 py-3.5">
        <h3 className="text-sm font-semibold">视频帧采样（逻辑采样）</h3>
        <p className="m-0 mt-1 text-xs text-muted-foreground">
          采样只约束「逐帧导航 +
          打点」的网格，不会物理重采样或取代原视频；连续播放仍走原始帧率与所有帧。
        </p>
      </div>
      <div className="p-4">
        <div className={ROW_CLASS}>
          <span className={LABEL_CLASS}>采样方式</span>
          <div className="inline-flex flex-col gap-2 text-sm">
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="radio"
                name="video-sampling-mode"
                checked={draft.mode === "none"}
                onChange={() => setMode("none")}
              />
              <span>不采样（所有帧）</span>
            </label>
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="radio"
                name="video-sampling-mode"
                checked={draft.mode === "fps"}
                onChange={() => setMode("fps")}
              />
              <span>按目标 fps</span>
            </label>
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="radio"
                name="video-sampling-mode"
                checked={draft.mode === "step"}
                onChange={() => setMode("step")}
              />
              <span>按帧间隔</span>
            </label>
          </div>
        </div>

        {draft.mode === "fps" && (
          <div className={ROW_CLASS}>
            <label className={LABEL_CLASS} htmlFor="video-target-fps">
              目标 fps（&gt; 0）
            </label>
            <input
              id="video-target-fps"
              type="number"
              min={0}
              step="any"
              inputMode="decimal"
              value={draft.targetFps}
              onChange={(e) => setDraft((d) => ({ ...d, targetFps: e.target.value }))}
              onBlur={() => commit(draft)}
              placeholder="例：10"
              className={INPUT_CLASS}
            />
          </div>
        )}

        {draft.mode === "step" && (
          <div className={ROW_CLASS}>
            <label className={LABEL_CLASS} htmlFor="video-frame-step">
              帧间隔（整数，&ge; 1）
            </label>
            <input
              id="video-frame-step"
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={draft.frameStep}
              onChange={(e) => setDraft((d) => ({ ...d, frameStep: e.target.value }))}
              onBlur={() => commit(draft)}
              placeholder="例：5"
              className={INPUT_CLASS}
            />
          </div>
        )}

        <div
          className="mt-3 rounded-md border border-border bg-muted px-3 py-2.5 text-sm text-muted-foreground"
          data-testid="video-sampling-preview"
        >
          {previewText(draft)}
        </div>

        {!valid && (
          <p className="text-xs text-status-danger">请填写合法的采样参数，填好后自动保存。</p>
        )}

        <div className="mt-4 border-t border-border pt-4">
          <h4 className="text-sm font-semibold text-foreground">长视频分段协同</h4>
          <p className="mt-1 text-xs text-muted-foreground">
            开启后，标注员按分段认领并在相邻重叠帧独立标注；已有标注后配置会冻结。
          </p>
          <label className="mt-3 flex min-h-10 cursor-pointer items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={collaboration.enabled ?? false}
              disabled={update.isPending}
              onChange={(event) =>
                commitCollaboration({
                  enabled: event.target.checked,
                  overlap_frames: event.target.checked
                    ? Math.max(1, collaboration.overlap_frames ?? 0)
                    : 0,
                })
              }
            />
            启用 segment overlap 协同
          </label>
          {collaboration.enabled && (
            <div className={ROW_CLASS}>
              <label className={LABEL_CLASS} htmlFor="video-overlap-frames">
                相邻重叠帧数
              </label>
              <input
                id="video-overlap-frames"
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={collaboration.overlap_frames ?? 1}
                disabled={update.isPending}
                onChange={(event) =>
                  setCollaboration((current) => ({
                    ...current,
                    overlap_frames: Number(event.target.value),
                  }))
                }
                onBlur={() => {
                  const overlap = collaboration.overlap_frames ?? 0;
                  if (Number.isInteger(overlap) && overlap > 0) {
                    commitCollaboration({ enabled: true, overlap_frames: overlap });
                  }
                }}
                className={INPUT_CLASS}
              />
              <span className="text-xs text-muted-foreground">
                重叠区必须包含至少一个当前采样网格帧，且小于服务端 segment 大小。
              </span>
            </div>
          )}
        </div>

        {update.isPending && <div className="mt-3.5 text-xs text-muted-foreground">保存中…</div>}
      </div>
    </Card>
  );
}
