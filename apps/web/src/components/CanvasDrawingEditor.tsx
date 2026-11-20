import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import type { CommentCanvasDrawing } from "@/api/comments";
import { randomId } from "@/utils/id";
import styles from "./CanvasDrawingEditor.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  onSave: (drawing: CommentCanvasDrawing | null) => void;
  /** Report the composed drawing immediately, including an in-progress pointer. */
  onDraftChange?: (drawing: CommentCanvasDrawing | null) => void;
  initial?: CommentCanvasDrawing | null;
  /** 背景图（可选）：reviewer 在原图缩略上绘制更直观；未提供则白底。 */
  backgroundUrl?: string | null;
  /** 图像真实尺寸（像素）。给定后画布外层比例按 imageWidth:imageHeight 渲染，
   *  避免在 16:9 / 4:3 / 1:1 图上画的批注与 reviewer 看到的比例不一致。
   *  未给定时回退到 600×400 默认比例（兼容旧调用）。*/
  imageWidth?: number | null;
  imageHeight?: number | null;
}

const STROKE_COLORS = [
  { value: "#ef4444", label: "红" },
  { value: "#f59e0b", label: "黄" },
  { value: "#10b981", label: "绿" },
  { value: "#3b82f6", label: "蓝" },
];

// 默认画布比例（旧版固定 600×400，仍保留作为 fallback）
const DEFAULT_W = 600;
const DEFAULT_H = 400;

function aspectRatioPercent(w: number | null | undefined, h: number | null | undefined): number {
  const aw = w && w > 0 ? w : DEFAULT_W;
  const ah = h && h > 0 ? h : DEFAULT_H;
  return (ah / aw) * 100;
}

/** Reviewer 用：在固定尺寸 SVG 上画自由曲线，序列化为 normalized [0,1] 坐标的 polyline 列表。
 *  Annotator 端用 CanvasDrawingPreview 只读渲染。 */
type Shape = NonNullable<CommentCanvasDrawing["shapes"]>[number];

function cloneShapes(shapes: readonly Shape[]): Shape[] {
  return shapes.map((shape) => ({ ...shape, points: [...shape.points] }));
}

export function CanvasDrawingEditor({
  open,
  onClose,
  onSave,
  onDraftChange,
  initial,
  backgroundUrl,
  imageWidth,
  imageHeight,
}: Props) {
  const [shapes, setShapes] = useState<Shape[]>(initial?.shapes ?? []);
  const [stroke, setStroke] = useState<string>("#ef4444");
  const [drawing, setDrawing] = useState<number[] | null>(null); // 当前正在画的折线点 [x1, y1, x2, y2, ...]
  const shapesRef = useRef<Shape[]>(initial?.shapes ?? []);
  const drawingRef = useRef<number[] | null>(null);
  const strokeRef = useRef(stroke);
  const drawingIdRef = useRef<string | null>(null);
  const wasOpenRef = useRef(false);
  // 同步标记是否正在绘制：pointerdown 里同步置位，不受 React 渲染时机影响。
  // 不能用闭包里的 `drawing` 做 move 守卫——pointerdown 的 setDrawing 尚未 flush 时，
  // 紧跟的快速 pointermove 会命中旧闭包 (drawing===null) 被丢弃，导致笔画开头缺失/跟不上手。
  const drawingActiveRef = useRef(false);
  const strokeStartedAtRef = useRef<number | null>(null); // 当前笔画起点 (Unix epoch ms)
  const svgRef = useRef<SVGSVGElement | null>(null);
  const canvasRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(
      () =>
        ({
          "--canvas-drawing-aspect-padding": `${aspectRatioPercent(imageWidth, imageHeight)}%`,
          "--canvas-drawing-bg": backgroundUrl
            ? `center/contain no-repeat url(${backgroundUrl})`
            : "var(--sc-muted)",
        }) as CSSProperties,
      [backgroundUrl, imageHeight, imageWidth],
    ),
  );

  // Open is a hydration boundary. While the modal is open, autosave updates
  // `initial` on every pointer event, but must not replace local state and
  // steal the in-progress stroke or move the pointer between renders.
  useEffect(() => {
    if (open && !wasOpenRef.current) {
      const nextShapes = cloneShapes(initial?.shapes ?? []);
      shapesRef.current = nextShapes;
      drawingRef.current = null;
      drawingIdRef.current = null;
      setShapes(nextShapes);
      setDrawing(null);
      drawingActiveRef.current = false;
    }
    wasOpenRef.current = open;
  }, [open, initial]);

  const composeDrawing = useCallback(
    (nextShapes = shapesRef.current, nextDrawing = drawingRef.current) => {
      const composed = [...nextShapes];
      if (nextDrawing && nextDrawing.length >= 2) {
        composed.push({
          type: "line",
          points: [...nextDrawing],
          stroke: strokeRef.current,
          id: drawingIdRef.current,
          started_at: strokeStartedAtRef.current,
          ended_at: null,
        });
      }
      return composed.length > 0 ? { shapes: cloneShapes(composed) } : null;
    },
    [],
  );

  const reportDraft = useCallback(
    (nextShapes = shapesRef.current, nextDrawing = drawingRef.current) => {
      onDraftChange?.(composeDrawing(nextShapes, nextDrawing));
    },
    [composeDrawing, onDraftChange],
  );

  const toNormalized = useCallback((e: React.PointerEvent<SVGSVGElement>): [number, number] => {
    const svg = svgRef.current;
    if (!svg) return [0, 0];
    const rect = svg.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    return [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))];
  }, []);

  const handleDown = (e: React.PointerEvent<SVGSVGElement>) => {
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const [x, y] = toNormalized(e);
    drawingActiveRef.current = true;
    const nextDrawing = [x, y];
    drawingRef.current = nextDrawing;
    drawingIdRef.current = randomId();
    setDrawing(nextDrawing);
    strokeStartedAtRef.current = Date.now();
    reportDraft(shapesRef.current, nextDrawing);
  };

  const handleMove = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!drawingActiveRef.current) return;
    const [x, y] = toNormalized(e);
    const current = drawingRef.current;
    if (!current) return;
    const nextDrawing = [...current, x, y];
    drawingRef.current = nextDrawing;
    setDrawing(nextDrawing);
    reportDraft(shapesRef.current, nextDrawing);
  };

  const handleUp = () => {
    drawingActiveRef.current = false;
    const currentDrawing = drawingRef.current;
    let nextShapes = shapesRef.current;
    if (currentDrawing && currentDrawing.length >= 4) {
      const startedAt = strokeStartedAtRef.current ?? Date.now();
      const endedAt = Date.now();
      nextShapes = [
        ...shapesRef.current,
        {
          type: "line",
          points: [...currentDrawing],
          stroke: strokeRef.current,
          id: drawingIdRef.current ?? randomId(),
          started_at: startedAt,
          ended_at: endedAt,
        },
      ];
      shapesRef.current = nextShapes;
      setShapes(nextShapes);
    }
    strokeStartedAtRef.current = null;
    drawingRef.current = null;
    drawingIdRef.current = null;
    setDrawing(null);
    reportDraft(nextShapes, null);
  };

  const handleClear = () => {
    const nextShapes: Shape[] = [];
    shapesRef.current = nextShapes;
    drawingRef.current = null;
    drawingIdRef.current = null;
    drawingActiveRef.current = false;
    strokeStartedAtRef.current = null;
    setShapes(nextShapes);
    setDrawing(null);
    reportDraft(nextShapes, null);
  };

  const handleUndo = () => {
    const nextShapes = shapesRef.current.slice(0, -1);
    shapesRef.current = nextShapes;
    setShapes(nextShapes);
    reportDraft(nextShapes);
  };

  const handleSave = () => {
    const nextDrawing = composeDrawing();
    onDraftChange?.(nextDrawing);
    onSave(nextDrawing);
    onClose();
  };

  const handleClose = () => {
    // Radix unmounts the modal body on Escape/close. The synchronous draft
    // channel is the last chance to retain a pointer that has not completed.
    reportDraft();
    onClose();
  };

  return (
    <Modal open={open} onClose={handleClose} title="画布批注" width={680} stopEscapePropagation>
      <div
        className={styles.editor}
        data-workbench-discussion
        data-state={open ? "open" : "closed"}
      >
        <div className={styles.toolbar}>
          <span className={styles.muted}>颜色：</span>
          {STROKE_COLORS.map((c) => (
            <button
              key={c.value}
              type="button"
              onClick={() => {
                strokeRef.current = c.value;
                setStroke(c.value);
              }}
              aria-label={c.label}
              className={stroke === c.value ? styles.swatchActive : styles.swatch}
              data-color={c.value}
            />
          ))}
          <span className={styles.hint}>按住鼠标拖动绘制 · {shapes.length} 条线</span>
        </div>
        <div ref={canvasRef} className={styles.canvas}>
          <svg
            ref={svgRef}
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            className={styles.drawingSvg}
            data-testid="canvas-drawing-editor"
            onPointerDown={handleDown}
            onPointerMove={handleMove}
            onPointerUp={handleUp}
            onPointerCancel={handleUp}
          >
            {shapes.map((s, i) => (
              <polyline
                key={i}
                points={pointsToString(s.points)}
                fill="none"
                stroke={s.stroke ?? "#ef4444"}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {drawing && drawing.length >= 4 && (
              <polyline
                points={pointsToString(drawing)}
                fill="none"
                stroke={stroke}
                strokeWidth={2}
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
            )}
          </svg>
        </div>
        <div className={styles.actions}>
          <Button size="sm" onClick={handleUndo} disabled={shapes.length === 0}>
            <Icon name="trash" size={11} /> 撤销
          </Button>
          <Button size="sm" onClick={handleClear} disabled={shapes.length === 0}>
            清空
          </Button>
          <Button size="sm" variant="primary" onClick={handleSave}>
            保存批注
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function pointsToString(pts: number[]): string {
  const out: string[] = [];
  for (let i = 0; i < pts.length; i += 2) {
    out.push(`${pts[i]},${pts[i + 1]}`);
  }
  return out.join(" ");
}

interface PreviewProps {
  drawing: CommentCanvasDrawing;
  width?: number;
  backgroundUrl?: string | null;
  /** 图像真实尺寸；给定后高度按真实比例缩放。fallback 600×400。*/
  imageWidth?: number | null;
  imageHeight?: number | null;
}

/** 只读小缩略：annotator 端在评论卡片里展示 reviewer 的画布批注。
 *  若所有 shape 都带 started_at/ended_at, 下方渲染迷你 timeline bar:
 *  每段颜色 = stroke 颜色, 宽度 ∝ 持续时长; hover 段 → 仅该 stroke 高亮(其他 dim). */
export function CanvasDrawingPreview({
  drawing,
  width = 220,
  backgroundUrl,
  imageWidth,
  imageHeight,
}: PreviewProps) {
  const aw = imageWidth && imageWidth > 0 ? imageWidth : DEFAULT_W;
  const ah = imageHeight && imageHeight > 0 ? imageHeight : DEFAULT_H;
  const height = (ah / aw) * width;
  const previewRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(
      () =>
        ({
          "--canvas-drawing-preview-width": width,
          "--canvas-drawing-preview-height": height,
          "--canvas-drawing-bg": backgroundUrl
            ? `center/contain no-repeat url(${backgroundUrl})`
            : "var(--sc-muted)",
        }) as CSSProperties,
      [backgroundUrl, height, width],
    ),
  );

  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const shapes = useMemo(() => drawing.shapes ?? [], [drawing.shapes]);
  const timelineData = useMemo(() => {
    const segments: { idx: number; color: string; durationMs: number }[] = [];
    for (let i = 0; i < shapes.length; i += 1) {
      const s = shapes[i];
      if (s.started_at == null || s.ended_at == null) return null;
      const duration = Math.max(1, s.ended_at - s.started_at);
      segments.push({ idx: i, color: s.stroke ?? "#ef4444", durationMs: duration });
    }
    return segments.length > 0 ? segments : null;
  }, [shapes]);

  return (
    <div>
      <div ref={previewRef} className={styles.preview}>
        <svg viewBox="0 0 1 1" preserveAspectRatio="none" className={styles.previewSvg}>
          {shapes.map((s, i) => (
            <polyline
              key={s.id ?? i}
              points={pointsToString(s.points)}
              fill="none"
              stroke={s.stroke ?? "#ef4444"}
              vectorEffect="non-scaling-stroke"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              opacity={hoveredIdx == null || hoveredIdx === i ? 1 : 0.25}
            />
          ))}
        </svg>
      </div>
      {timelineData && (
        <TimelineBar
          width={width}
          segments={timelineData}
          hoveredIdx={hoveredIdx}
          onHover={setHoveredIdx}
        />
      )}
    </div>
  );
}

interface TimelineBarProps {
  width: number;
  segments: { idx: number; color: string; durationMs: number }[];
  hoveredIdx: number | null;
  onHover: (idx: number | null) => void;
}

function TimelineBar({ width, segments, hoveredIdx, onHover }: TimelineBarProps) {
  const total = segments.reduce((sum, s) => sum + s.durationMs, 0) || 1;
  const wrapperRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(
      () =>
        ({
          "--canvas-drawing-preview-width": `${width}px`,
        }) as CSSProperties,
      [width],
    ),
  );
  return (
    <div ref={wrapperRef} className={styles.timelineWrapper} data-testid="canvas-drawing-timeline">
      <span className={styles.timelineLabel}>笔画 timeline · {segments.length} 段</span>
      <div className={styles.timelineBar}>
        {segments.map((seg) => (
          <TimelineSegment
            key={seg.idx}
            grow={(seg.durationMs / total) * 100}
            color={seg.color}
            dimmed={hoveredIdx != null && hoveredIdx !== seg.idx}
            onEnter={() => onHover(seg.idx)}
            onLeave={() => onHover(null)}
          />
        ))}
      </div>
    </div>
  );
}

interface TimelineSegmentProps {
  grow: number;
  color: string;
  dimmed: boolean;
  onEnter: () => void;
  onLeave: () => void;
}

function TimelineSegment({ grow, color, dimmed, onEnter, onLeave }: TimelineSegmentProps) {
  const segRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(
      () =>
        ({
          "--seg-grow": grow,
          "--seg-color": color,
        }) as CSSProperties,
      [grow, color],
    ),
  );
  return (
    <div
      ref={segRef}
      className={`${styles.timelineSegment}${dimmed ? " " + styles.timelineSegmentDim : ""}`}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      data-testid="canvas-drawing-timeline-segment"
    />
  );
}
