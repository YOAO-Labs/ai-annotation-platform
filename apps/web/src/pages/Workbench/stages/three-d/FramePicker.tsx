/**
 * v0.15.20 · 跨帧延续 / 区间插值的「目标帧」选择器。
 *
 * 替代 CrossFrameInterpolateBar 的裸数字输入:
 * - Layer 1:回显当前 / 目标帧在 scene 内的位置 + 语义步进(+5 / +10 / 到末帧)+ 数字兜底;
 *   目标 task 经 neighbors(k=20) 反查,超 ±20 帧给出不可达提示。
 * - Layer 2:相机图缩略图条(±5 邻帧),点缩略图即设目标帧;无相机图的帧降级为占位,
 *   整条无图时不渲染(回落纯 Layer 1)。
 * 由 3D 画布右键菜单的「延续到指定帧 / 向后插值填充」触发,fixed 定位在鼠标点。
 * v0.17.6 · module.css → Tailwind。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useFrameNeighbors } from "@/hooks/useFrameNeighbors";
import { Button } from "@/components/ui/Button";
import { useCameraOriginalImage } from "./useCameraOriginalImage";
import { useNeighborFrameThumbs } from "./useNeighborFrameThumbs";

export type FramePickerMode = "propagate" | "interpolate";

// v0.17.6 · Tailwind class constants (was FramePicker.module.css).
const POPOVER =
  "fixed left-[var(--frame-picker-left)] top-[var(--frame-picker-top)] z-workbench-modal w-[280px] max-w-[90vw] p-3 flex flex-col gap-2.5 bg-card border border-border rounded-lg shadow-lg text-sm text-foreground";
const TITLE = "font-semibold text-sm text-foreground";
const HINT = "text-xs text-muted-foreground";
const FILMSTRIP = "flex gap-1.5 overflow-x-auto pb-1";
const THUMB =
  "shrink-0 flex flex-col items-center gap-0.5 p-0.5 bg-background border border-border rounded-md cursor-pointer hover:border-foreground/20 hover:bg-muted";
const THUMB_ACTIVE = "!border-brand";
const THUMB_CURRENT = "outline outline-2 outline-brand/10";
const THUMB_IMG = "w-14 h-10 object-cover rounded block";
const THUMB_PLACEHOLDER =
  "w-14 h-10 flex items-center justify-center text-muted-foreground bg-muted rounded";
const THUMB_NUM = "text-xs text-muted-foreground";
const STEPS = "flex gap-1.5";
const CHIP =
  "flex-1 px-2 py-1 text-xs bg-background border border-border rounded-md text-foreground cursor-pointer appearance-none hover:border-foreground/20 hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed";
const LABEL = "flex items-center gap-2 text-xs text-muted-foreground";
const INPUT =
  "flex-1 px-2 py-1 text-sm bg-background border border-border rounded-md text-foreground appearance-none focus:outline-none focus:border-brand";
const FEEDBACK = "text-xs min-h-4";
const OK = "text-status-positive";
const WARN = "text-status-caution";
const ACTIONS = "flex justify-end gap-2";

interface FramePickerProps {
  taskId: string;
  frameIndex: number | null;
  sceneTotalFrames: number | null;
  mode: FramePickerMode;
  anchor: { left: number; top: number };
  onConfirm: (resolved: { targetTaskId: string; targetFrame: number }) => void;
  onCancel: () => void;
  pushToast: (t: { msg: string; kind?: "success" | "warning" | "error" | "" }) => void;
}

export function FramePicker({
  taskId,
  frameIndex,
  sceneTotalFrames,
  mode,
  anchor,
  onConfirm,
  onCancel,
  pushToast,
}: FramePickerProps) {
  const ref = useRef<HTMLDivElement>(null);
  // 展开即拉 k=20 邻帧(目标帧 → task 反查表)。
  const { data: neighbors } = useFrameNeighbors(taskId, 20);
  const taskByFrame = useMemo(() => {
    const m = new Map<number, string>();
    for (const n of [...(neighbors?.prev ?? []), ...(neighbors?.next ?? [])])
      m.set(n.frame_index, n.task_id);
    return m;
  }, [neighbors]);

  // Layer 2 · 相机图缩略图条(±5 邻帧;无相机图时整条不渲染,回落纯 Layer 1)。
  const thumbs = useNeighborFrameThumbs(taskId, frameIndex, 5, true);

  const maxFrame = sceneTotalFrames != null ? sceneTotalFrames - 1 : null;
  const suggested = frameIndex != null ? Math.min(frameIndex + 5, maxFrame ?? frameIndex + 5) : 0;
  const [raw, setRaw] = useState(String(suggested));
  const target = raw === "" ? null : Number(raw);
  const targetValid = target != null && Number.isInteger(target);
  const reachable = targetValid && target !== frameIndex && taskByFrame.has(target);

  // fixed 定位 + 视口边界回拉(参考 ClassPickerPopover 的 fixed clamp)。
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const m = 8;
    const r = el.getBoundingClientRect();
    let l = anchor.left;
    let t = anchor.top;
    if (l + r.width > window.innerWidth - m) l = window.innerWidth - m - r.width;
    if (l < m) l = m;
    if (t + r.height > window.innerHeight - m) t = Math.max(m, window.innerHeight - m - r.height);
    if (t < m) t = m;
    el.style.setProperty("--frame-picker-left", `${l}px`);
    el.style.setProperty("--frame-picker-top", `${t}px`);
  }, [anchor]);

  // Esc / 点击外部取消(延迟绑定 mousedown,避免捕获到打开这一次的点击)。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onCancel();
    };
    window.addEventListener("keydown", onKey);
    const t = setTimeout(() => document.addEventListener("mousedown", onDown), 0);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(t);
      document.removeEventListener("mousedown", onDown);
    };
  }, [onCancel]);

  const stepTo = (frame: number) => {
    if (maxFrame == null) return;
    setRaw(String(Math.min(maxFrame, Math.max(0, frame))));
  };

  const confirm = () => {
    if (!targetValid) {
      pushToast({ msg: "请填写目标帧号", kind: "" });
      return;
    }
    if (target === frameIndex) {
      pushToast({ msg: "目标帧不能是当前帧", kind: "" });
      return;
    }
    if (maxFrame != null && (target < 0 || target > maxFrame)) {
      pushToast({ msg: `目标帧超出范围 (0–${maxFrame})`, kind: "" });
      return;
    }
    const tid = taskByFrame.get(target);
    if (!tid) {
      pushToast({ msg: "目标帧超出 ±20 帧反查范围, 请先跳到更近的帧分段操作", kind: "warning" });
      return;
    }
    onConfirm({ targetTaskId: tid, targetFrame: target });
  };

  return (
    <div
      ref={ref}
      className={POPOVER}
      role="dialog"
      aria-label={mode === "propagate" ? "延续到指定帧" : "插值填充到指定帧"}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className={TITLE}>{mode === "propagate" ? "延续到指定帧" : "插值填充到指定帧"}</div>
      <div className={HINT}>
        当前 第 {frameIndex ?? "?"} / {sceneTotalFrames ?? "?"} 帧
      </div>
      {thumbs.some((t) => t.imageUrl) && (
        <div className={FILMSTRIP}>
          {thumbs.map((t) => (
            <button
              key={t.frameIndex}
              type="button"
              className={[
                THUMB,
                t.frameIndex === target ? THUMB_ACTIVE : "",
                t.isCurrent ? THUMB_CURRENT : "",
              ]
                .filter(Boolean)
                .join(" ")}
              title={`第 ${t.frameIndex} 帧${t.isCurrent ? " · 当前" : ""}`}
              onClick={() => setRaw(String(t.frameIndex))}
            >
              {t.imageUrl ? (
                <CameraThumbnail source={t.imageUrl} frameIndex={t.frameIndex} />
              ) : (
                <span className={THUMB_PLACEHOLDER}>—</span>
              )}
              <span className={THUMB_NUM}>{t.frameIndex}</span>
            </button>
          ))}
        </div>
      )}
      <div className={STEPS}>
        <button
          type="button"
          className={CHIP}
          disabled={frameIndex == null}
          onClick={() => frameIndex != null && stepTo(frameIndex + 5)}
        >
          +5
        </button>
        <button
          type="button"
          className={CHIP}
          disabled={frameIndex == null}
          onClick={() => frameIndex != null && stepTo(frameIndex + 10)}
        >
          +10
        </button>
        <button
          type="button"
          className={CHIP}
          disabled={maxFrame == null}
          onClick={() => maxFrame != null && stepTo(maxFrame)}
        >
          到末帧
        </button>
      </div>
      <label className={LABEL}>
        目标帧
        <input
          type="number"
          className={INPUT}
          min={0}
          max={maxFrame ?? undefined}
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") confirm();
          }}
        />
      </label>
      <div className={FEEDBACK}>
        {!targetValid ? (
          <span className={HINT}>输入或步进选择目标帧</span>
        ) : reachable ? (
          <span className={OK}>
            → 第 {target} / {sceneTotalFrames} 帧 · 可达
          </span>
        ) : (
          <span className={WARN}>第 {target} 帧不可达(同帧或超 ±20 反查范围)</span>
        )}
      </div>
      <div className={ACTIONS}>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          取消
        </Button>
        <Button variant="primary" size="sm" disabled={!reachable} onClick={confirm}>
          {mode === "propagate" ? "延续到此帧" : "插值填充"}
        </Button>
      </div>
    </div>
  );
}

function CameraThumbnail({ source, frameIndex }: { source: string; frameIndex: number }) {
  const { url } = useCameraOriginalImage(source);
  return url ? (
    <img src={url} alt={`第 ${frameIndex} 帧`} className={THUMB_IMG} loading="lazy" />
  ) : (
    <span className={THUMB_PLACEHOLDER}>—</span>
  );
}
