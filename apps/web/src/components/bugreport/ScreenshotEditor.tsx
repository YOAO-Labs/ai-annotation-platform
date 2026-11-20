/**
 * BugReportDrawer 截图 + 涂抹遮罩。
 *
 * 流程：父组件传 imageBlob (html2canvas 输出) → 本组件画到 canvas → 用户拖拽
 * 黑色矩形遮挡敏感区 → 点「确认」回写合并后的 dataUrl + blob 给父。
 *
 * 为避免引入额外画布库，直接用 2D context drawRect。仅支持单方向拖拽矩形，
 * 多于一条 → 维护 rects 数组并按顺序绘制。
 */
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import styles from "./ScreenshotEditor.module.css";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Props {
  imageBlob: Blob;
  onConfirm: (blob: Blob) => void;
  onCancel: () => void;
}

export function ScreenshotEditor({ imageBlob, onConfirm, onCancel }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const [bgImage, setBgImage] = useState<HTMLImageElement | null>(null);
  const [rects, setRects] = useState<Rect[]>([]);
  const [drag, setDrag] = useState<{ x0: number; y0: number; cur: Rect } | null>(null);
  const [previewSize, setPreviewSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  // 加载 blob → image
  useEffect(() => {
    const url = URL.createObjectURL(imageBlob);
    const img = new Image();
    img.onload = () => {
      setBgImage(img);
      const maxW = 720;
      const scale = img.naturalWidth > maxW ? maxW / img.naturalWidth : 1;
      setPreviewSize({ w: img.naturalWidth * scale, h: img.naturalHeight * scale });
    };
    img.src = url;
    return () => URL.revokeObjectURL(url);
  }, [imageBlob]);

  // 绘制：背景 + 已落地矩形 + 当前拖拽矩形
  useEffect(() => {
    if (!bgImage || !canvasRef.current) return;
    const cv = canvasRef.current;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    cv.width = bgImage.naturalWidth;
    cv.height = bgImage.naturalHeight;
    ctx.drawImage(bgImage, 0, 0);
    ctx.fillStyle = "#000";
    rects.forEach((r) => ctx.fillRect(r.x, r.y, r.w, r.h));
    if (drag) {
      ctx.fillRect(drag.cur.x, drag.cur.y, drag.cur.w, drag.cur.h);
    }
  }, [bgImage, rects, drag]);

  useEffect(() => {
    const preview = previewRef.current;
    if (!preview) return;
    preview.style.setProperty(
      "--bug-screenshot-preview-width",
      previewSize.w ? `${previewSize.w}px` : "100%",
    );
    preview.style.setProperty(
      "--bug-screenshot-preview-height",
      previewSize.h ? `${previewSize.h}px` : "200px",
    );
  }, [previewSize]);

  const toCanvasCoords = (e: React.MouseEvent) => {
    const cv = canvasRef.current!;
    const rect = cv.getBoundingClientRect();
    const sx = cv.width / rect.width;
    const sy = cv.height / rect.height;
    return { x: (e.clientX - rect.left) * sx, y: (e.clientY - rect.top) * sy };
  };

  const handleDown = (e: React.MouseEvent) => {
    const p = toCanvasCoords(e);
    setDrag({ x0: p.x, y0: p.y, cur: { x: p.x, y: p.y, w: 0, h: 0 } });
  };
  const handleMove = (e: React.MouseEvent) => {
    if (!drag) return;
    const p = toCanvasCoords(e);
    setDrag({
      ...drag,
      cur: {
        x: Math.min(drag.x0, p.x),
        y: Math.min(drag.y0, p.y),
        w: Math.abs(p.x - drag.x0),
        h: Math.abs(p.y - drag.y0),
      },
    });
  };
  const handleUp = () => {
    if (!drag) return;
    if (drag.cur.w > 4 && drag.cur.h > 4) setRects((rs) => [...rs, drag.cur]);
    setDrag(null);
  };

  const handleConfirm = () => {
    if (!canvasRef.current) return;
    canvasRef.current.toBlob((blob) => {
      if (blob) onConfirm(blob);
    }, "image/png");
  };

  return (
    <div className={styles.root}>
      <div className={styles.hint}>
        在截图上拖拽鼠标 → 黑色矩形遮挡敏感区。完成后点「确认」上传。
      </div>
      <div ref={previewRef} className={styles.preview}>
        <canvas
          ref={canvasRef}
          onMouseDown={handleDown}
          onMouseMove={handleMove}
          onMouseUp={handleUp}
          onMouseLeave={handleUp}
          className={styles.canvas}
        />
      </div>
      <div className={styles.actions}>
        {rects.length > 0 && (
          <button
            type="button"
            onClick={() => setRects((rs) => rs.slice(0, -1))}
            className={`${styles.button} ${styles.ghostButton}`}
          >
            撤销最后一框
          </button>
        )}
        <button
          type="button"
          onClick={onCancel}
          className={`${styles.button} ${styles.ghostButton}`}
        >
          取消
        </button>
        <button
          type="button"
          onClick={handleConfirm}
          className={`${styles.button} ${styles.primaryButton}`}
        >
          <Icon name="check" size={11} /> 确认
        </button>
      </div>
    </div>
  );
}
