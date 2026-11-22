import { useEffect, useRef } from "react";
import styles from "./BlurhashLayer.module.css";

export function BlurhashLayer({ hash }: { hash: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (!canvasRef.current) return;
    let cancelled = false;
    import("blurhash")
      .then(({ decode }) => {
        if (cancelled || !canvasRef.current) return;
        const W = 32,
          H = 24;
        const pixels = decode(hash, W, H);
        const canvas = canvasRef.current;
        canvas.width = W;
        canvas.height = H;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        const imageData = ctx.createImageData(W, H);
        imageData.data.set(pixels);
        ctx.putImageData(imageData, 0, 0);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hash]);

  return <canvas ref={canvasRef} className={styles.canvas} />;
}
