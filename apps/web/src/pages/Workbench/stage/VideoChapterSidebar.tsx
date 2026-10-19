import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import {
  useCreateVideoChapter,
  useDeleteVideoChapter,
  useUpdateVideoChapter,
  useVideoChapters,
} from "@/hooks/useVideoChapters";

import type { FrameTimebase } from "./frameTimebase";

const CHAPTER_PALETTE = [
  "oklch(0.62 0.18 252)",
  "oklch(0.65 0.18 152)",
  "oklch(0.68 0.16 75)",
  "oklch(0.62 0.20 25)",
  "oklch(0.60 0.20 295)",
];

interface VideoChapterSidebarProps {
  datasetItemId: string | null;
  frameIndex: number;
  maxFrame: number;
  timebase?: FrameTimebase;
  canEdit: boolean;
  onSeekFrame?: (frameIndex: number) => void;
  /** v0.21.13 · 时间轴「圈选建章节」臂选态 (由 shell 管理, 圈一段后自动解除)。 */
  timelineDraftArmed?: boolean;
  onToggleTimelineDraft?: () => void;
  /** v0.21.13 · 时间轴刷选产物 (start/end 帧); 一变化即打开新建表单并预填范围。 */
  draftRange?: { startFrame: number; endFrame: number } | null;
  /** 消费完 draftRange 后通知 shell 清空, 使同一区间可再次触发。 */
  onConsumeDraftRange?: () => void;
  /** v0.21.13 WS4 · 时间轴章节条 ↔ 侧栏行双向 hover 联动。 */
  hoveredChapterId?: string | null;
  onHoverChapter?: (chapterId: string | null) => void;
}

function formatChapterDuration(start: number, end: number, timebase?: FrameTimebase) {
  if (!timebase || !Number.isFinite(timebase.fps) || timebase.fps <= 0) {
    return `${end - start + 1} 帧`;
  }
  const seconds = (end - start + 1) / timebase.fps;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function defaultChapterColor(index: number): string {
  return CHAPTER_PALETTE[index % CHAPTER_PALETTE.length];
}

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

interface ChapterFormState {
  chapterId: string | null;
  title: string;
  startFrame: number;
  endFrame: number;
  color: string;
}

function ColorSwatch({
  color,
  selected,
  onSelect,
}: {
  color: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const ref = useElementStyle<HTMLButtonElement>({ backgroundColor: color } as CSSProperties);
  return (
    <button
      ref={ref}
      type="button"
      onClick={onSelect}
      className={cn(
        "w-5 h-5 border border-border rounded-full cursor-pointer p-0",
        selected && "border-2 border-foreground",
      )}
      aria-label={`color ${color}`}
    />
  );
}

export function VideoChapterSidebar({
  datasetItemId,
  frameIndex,
  maxFrame,
  timebase,
  canEdit,
  onSeekFrame,
  timelineDraftArmed = false,
  onToggleTimelineDraft,
  draftRange = null,
  onConsumeDraftRange,
  hoveredChapterId = null,
  onHoverChapter,
}: VideoChapterSidebarProps) {
  const { data: chapters = [], isLoading } = useVideoChapters(datasetItemId);
  const createMutation = useCreateVideoChapter(datasetItemId);
  const updateMutation = useUpdateVideoChapter(datasetItemId);
  const deleteMutation = useDeleteVideoChapter(datasetItemId);

  const [editing, setEditing] = useState<ChapterFormState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
  }, [editing?.chapterId]);

  // v0.21.13 · 时间轴刷选产物到达 → 打开新建表单并预填起止帧 (只剩填标题 / 选色), 随即通知 shell 清空。
  useEffect(() => {
    if (!draftRange) return;
    const start = Math.max(0, Math.min(draftRange.startFrame, draftRange.endFrame));
    const end = Math.min(maxFrame, Math.max(draftRange.startFrame, draftRange.endFrame));
    setEditing({
      chapterId: null,
      title: "",
      startFrame: start,
      endFrame: end,
      color: defaultChapterColor(chapters.length),
    });
    onConsumeDraftRange?.();
    // 仅在 draftRange 变化时触发; 消费后 shell 会把它清空, guard 提前返回避免重入。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftRange]);

  const sortedChapters = useMemo(
    () => [...chapters].sort((a, b) => a.start_frame - b.start_frame),
    [chapters],
  );

  const startCreate = () => {
    const fallbackColor = defaultChapterColor(chapters.length);
    setEditing({
      chapterId: null,
      title: "",
      startFrame: frameIndex,
      endFrame: Math.min(maxFrame, frameIndex + Math.max(0, Math.round(maxFrame * 0.1))),
      color: fallbackColor,
    });
  };

  const startEdit = (chapterId: string) => {
    const chapter = chapters.find((c) => c.id === chapterId);
    if (!chapter) return;
    setEditing({
      chapterId: chapter.id,
      title: chapter.title,
      startFrame: chapter.start_frame,
      endFrame: chapter.end_frame,
      color: chapter.color ?? defaultChapterColor(0),
    });
  };

  const cancelForm = () => {
    setEditing(null);
    setError(null);
  };

  const submitForm = async () => {
    if (!editing) return;
    const title = editing.title.trim();
    if (!title) {
      setError("标题不可为空");
      return;
    }
    if (editing.endFrame < editing.startFrame) {
      setError("结束帧必须 ≥ 起始帧");
      return;
    }
    const payload = {
      title,
      start_frame: editing.startFrame,
      end_frame: editing.endFrame,
      color: editing.color,
    };
    try {
      if (editing.chapterId) {
        await updateMutation.mutateAsync({
          chapterId: editing.chapterId,
          payload,
        });
      } else {
        await createMutation.mutateAsync(payload);
      }
      setEditing(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
    }
  };

  const onDelete = async (chapterId: string) => {
    try {
      await deleteMutation.mutateAsync(chapterId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败");
    }
  };

  if (!datasetItemId) return null;

  return (
    <div
      data-testid="video-chapter-sidebar"
      className="grid gap-2 p-2.5 px-3 border border-border rounded-lg bg-card"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-semibold">章节</span>
          <span className="mono text-xs font-medium text-muted-foreground">
            {sortedChapters.length}
          </span>
        </div>
        {canEdit && (
          <div className="flex items-center gap-1">
            {onToggleTimelineDraft && (
              <Button
                size="sm"
                variant={timelineDraftArmed ? "primary" : "ghost"}
                className="!py-1 !px-2 !rounded-lg"
                disabled={Boolean(editing)}
                onClick={onToggleTimelineDraft}
                title={timelineDraftArmed ? "取消时间轴圈选" : "在时间轴上拖选章节范围"}
                aria-pressed={timelineDraftArmed}
              >
                <Icon name="scan" size={13} />
                圈选
              </Button>
            )}
            <Button
              size="sm"
              className="!py-1 !px-2 !rounded-lg"
              disabled={Boolean(editing)}
              onClick={startCreate}
              title="新建章节"
            >
              <Icon name="plus" size={13} />
              新建
            </Button>
          </div>
        )}
      </div>

      {timelineDraftArmed && !editing && (
        <div
          data-testid="video-chapter-draft-hint"
          className="flex items-center gap-1.5 py-1.5 px-2 rounded-lg bg-brand/10 text-brand text-xs"
        >
          <Icon name="scan" size={12} />
          在时间轴上按住拖动圈选章节范围…
        </div>
      )}

      {isLoading && sortedChapters.length === 0 && (
        <div className="text-muted-foreground text-xs">载入中…</div>
      )}

      {sortedChapters.length === 0 && !isLoading && !editing && (
        <div className="text-muted-foreground text-xs leading-relaxed">
          暂无章节。用 PageDown / PageUp 在章节之间跳转。
        </div>
      )}

      <div className="grid gap-1.5">
        {sortedChapters.map((chapter, idx) => {
          const isInside = frameIndex >= chapter.start_frame && frameIndex <= chapter.end_frame;
          const isHovered = hoveredChapterId === chapter.id;
          const color = chapter.color ?? defaultChapterColor(idx);
          return (
            <div
              key={chapter.id}
              data-testid="video-chapter-row"
              data-hovered={isHovered ? "true" : undefined}
              aria-selected={isInside}
              onMouseEnter={() => onHoverChapter?.(chapter.id)}
              onMouseLeave={() => onHoverChapter?.(null)}
              className={cn(
                "grid grid-cols-[auto_minmax(0,1fr)_auto] gap-2 items-center py-2 px-2.5 border border-border rounded-lg bg-background",
                isInside && "!border-brand bg-brand/10",
                isHovered && !isInside && "!border-brand/60 bg-muted",
              )}
            >
              <svg className="w-2.5 h-2.5 rounded-full" viewBox="0 0 10 10" aria-hidden="true">
                <circle cx="5" cy="5" r="5" fill={color} />
              </svg>
              <button
                type="button"
                onClick={() => onSeekFrame?.(chapter.start_frame)}
                className="grid gap-0.5 items-start min-w-0 p-0 border-0 bg-transparent text-foreground cursor-pointer text-left"
              >
                <b className="overflow-hidden text-sm text-ellipsis whitespace-nowrap">
                  {idx + 1}. {chapter.title}
                  {chapter.source === "sampled" && (
                    <span
                      className="ml-1.5 px-1.5 py-px rounded-full bg-brand/10 text-brand text-2xs font-medium align-middle"
                      title="由采样网格派生"
                    >
                      采样
                    </span>
                  )}
                </b>
                <span className="mono text-muted-foreground text-xs">
                  F{chapter.start_frame}–F{chapter.end_frame} ·{" "}
                  {formatChapterDuration(chapter.start_frame, chapter.end_frame, timebase)}
                  {chapter.frame_step != null && ` · 步长 ${chapter.frame_step}`}
                </span>
              </button>
              {canEdit && (
                <div className="flex gap-1">
                  <Button
                    size="sm"
                    className="!justify-center !w-[26px] !h-[26px] !p-0 !rounded-lg"
                    title="编辑章节"
                    onClick={(e) => {
                      e.stopPropagation();
                      startEdit(chapter.id);
                    }}
                  >
                    <Icon name="edit" size={13} />
                  </Button>
                  <Button
                    size="sm"
                    className="!justify-center !w-[26px] !h-[26px] !p-0 !rounded-lg !text-status-danger"
                    title="删除章节"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(chapter.id);
                    }}
                  >
                    <Icon name="trash" size={13} />
                  </Button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {editing && (
        <div
          data-testid="video-chapter-form"
          className="grid gap-1.5 p-2 px-2.5 border border-brand rounded-lg bg-brand/5"
        >
          <input
            type="text"
            placeholder="章节标题"
            value={editing.title}
            onChange={(e) =>
              setEditing((prev) => (prev ? { ...prev, title: e.target.value } : prev))
            }
            className="py-1.5 px-2 text-sm border border-border rounded-md bg-background text-foreground"
          />
          <div className="grid grid-cols-2 gap-1.5 items-center">
            <label className="grid gap-0.5 text-muted-foreground text-xs">
              起始帧
              <div className="flex gap-1">
                <input
                  type="number"
                  min={0}
                  max={maxFrame}
                  value={editing.startFrame}
                  onChange={(e) =>
                    setEditing((prev) =>
                      prev ? { ...prev, startFrame: Number(e.target.value) } : prev,
                    )
                  }
                  className="flex-1 py-1 px-1.5 text-xs border border-border rounded-md bg-background text-foreground"
                />
                <Button
                  size="sm"
                  className="!px-1.5 !rounded-md"
                  title="使用当前帧"
                  onClick={() =>
                    setEditing((prev) => (prev ? { ...prev, startFrame: frameIndex } : prev))
                  }
                >
                  当前
                </Button>
              </div>
            </label>
            <label className="grid gap-0.5 text-muted-foreground text-xs">
              结束帧
              <div className="flex gap-1">
                <input
                  type="number"
                  min={0}
                  max={maxFrame}
                  value={editing.endFrame}
                  onChange={(e) =>
                    setEditing((prev) =>
                      prev ? { ...prev, endFrame: Number(e.target.value) } : prev,
                    )
                  }
                  className="flex-1 py-1 px-1.5 text-xs border border-border rounded-md bg-background text-foreground"
                />
                <Button
                  size="sm"
                  className="!px-1.5 !rounded-md"
                  title="使用当前帧"
                  onClick={() =>
                    setEditing((prev) => (prev ? { ...prev, endFrame: frameIndex } : prev))
                  }
                >
                  当前
                </Button>
              </div>
            </label>
          </div>
          <label className="grid gap-0.5 text-muted-foreground text-xs">
            颜色
            <div className="flex flex-wrap gap-1.5">
              {CHAPTER_PALETTE.map((c) => (
                <ColorSwatch
                  key={c}
                  color={c}
                  selected={editing.color === c}
                  onSelect={() => setEditing((prev) => (prev ? { ...prev, color: c } : prev))}
                />
              ))}
            </div>
          </label>
          {error && (
            <div className="text-status-danger text-xs">
              <CopyableError message={error} />
            </div>
          )}
          <div className="flex gap-1.5 justify-end">
            <Button size="sm" className="!rounded-md" variant="ghost" onClick={cancelForm}>
              取消
            </Button>
            <Button
              size="sm"
              className="!rounded-md"
              onClick={submitForm}
              disabled={createMutation.isPending || updateMutation.isPending}
            >
              {editing.chapterId ? "保存" : "创建"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function pickChapterTargetFrame(
  chapters: { id: string; start_frame: number; end_frame: number }[],
  currentFrame: number,
  direction: "next" | "prev",
): number | null {
  if (chapters.length === 0) return null;
  const sorted = [...chapters].sort((a, b) => a.start_frame - b.start_frame);
  if (direction === "next") {
    for (const c of sorted) {
      if (c.start_frame > currentFrame) return c.start_frame;
    }
    return null;
  }
  // prev：若处于章节内部（非起点），跳回当前章节起点；否则去上一章
  for (let i = sorted.length - 1; i >= 0; i--) {
    const c = sorted[i];
    if (currentFrame > c.start_frame && currentFrame <= c.end_frame) {
      return c.start_frame;
    }
    if (c.start_frame < currentFrame) return c.start_frame;
  }
  return null;
}
