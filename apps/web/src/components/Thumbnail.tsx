import { useState, useEffect, useMemo, useRef } from "react";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import styles from "./Thumbnail.module.css";

interface ThumbnailProps {
  src: string | null | undefined;
  blurhash?: string | null;
  alt?: string;
  fillContainer?: boolean;
  width?: number;
  height?: number;
  style?: React.CSSProperties;
}

export function Thumbnail({
  src,
  blurhash,
  alt = "",
  fillContainer = false,
  width = 48,
  height = 48,
  style,
}: ThumbnailProps) {
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rootStyle = useMemo<React.CSSProperties | undefined>(
    () => (fillContainer ? style : { width, height, ...style }),
    [fillContainer, height, style, width],
  );
  const rootRef = useElementStyle<HTMLDivElement>(rootStyle);
  const className = fillContainer ? `${styles.root} ${styles.fillContainer}` : styles.root;
  const showBlurhash = Boolean(blurhash && !loaded);
  const sampleWidth = fillContainer ? 48 : width;
  const sampleHeight = fillContainer ? 48 : height;

  useEffect(() => {
    setLoaded(false);
    setErrored(false);
  }, [blurhash, src]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!showBlurhash || !blurhash || !canvas) return;
    let current = true;
    import("blurhash")
      .then(({ decode }) => {
        if (!current) return;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        const pixels = decode(blurhash, sampleWidth, sampleHeight);
        const imageData = ctx.createImageData(sampleWidth, sampleHeight);
        imageData.data.set(pixels);
        ctx.putImageData(imageData, 0, 0);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [blurhash, sampleWidth, sampleHeight, showBlurhash]);

  return (
    <div ref={rootRef} className={className}>
      {showBlurhash && (
        <canvas
          ref={canvasRef}
          width={sampleWidth}
          height={sampleHeight}
          className={styles.media}
          aria-hidden
        />
      )}
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
