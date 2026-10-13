import { useState, useEffect, useMemo, useRef } from "react";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import styles from "./Thumbnail.module.css";

interface ThumbnailProps {
  src: string | null | undefined;
  blurhash?: string | null;
  alt?: string;
  width?: number;
  height?: number;
  style?: React.CSSProperties;
}

export function Thumbnail({
  src,
  blurhash,
  alt = "",
  width = 48,
  height = 48,
  style,
}: ThumbnailProps) {
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rootStyle = useMemo<React.CSSProperties>(
    () => ({
      width,
      height,
      ...style,
    }),
    [height, style, width],
  );
  const rootRef = useElementStyle<HTMLDivElement>(rootStyle);

  useEffect(() => {
    setLoaded(false);
    setErrored(false);
  }, [blurhash, src]);

  useEffect(() => {
    if (!blurhash || !canvasRef.current) return;
    import("blurhash")
      .then(({ decode }) => {
        const pixels = decode(blurhash, width, height);
        const canvas = canvasRef.current!;
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d")!;
        const imageData = ctx.createImageData(width, height);
        imageData.data.set(pixels);
        ctx.putImageData(imageData, 0, 0);
      })
      .catch(() => {
        /* ignore */
      });
  }, [blurhash, width, height]);

  if (!src && !blurhash) {
    return (
      <div ref={rootRef} className={styles.root}>
        <Icon name="image" size={14} className={styles.placeholderIcon} />
      </div>
    );
  }

  return (
    <div ref={rootRef} className={styles.root}>
      {/* blurhash canvas placeholder */}
      {blurhash && !loaded && <canvas ref={canvasRef} className={styles.media} />}
      {/* actual image */}
      {src && !errored && (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setErrored(true)}
          className={loaded ? styles.imageLoaded : styles.imageLoading}
        />
      )}
      {(!src || errored) && !blurhash && (
        <Icon name="image" size={14} className={styles.placeholderIcon} />
      )}
    </div>
  );
}
