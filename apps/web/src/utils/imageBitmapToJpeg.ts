async function drawableToJpeg(
  src: CanvasImageSource,
  width: number,
  height: number,
  quality: number,
): Promise<Blob> {
  if (typeof OffscreenCanvas !== "undefined") {
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("无法获取 2D 绘图上下文");
    ctx.drawImage(src, 0, 0);
    return canvas.convertToBlob({ type: "image/jpeg", quality });
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("无法获取 2D 绘图上下文");
  ctx.drawImage(src, 0, 0);
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("canvas.toBlob 返回空"))),
      "image/jpeg",
      quality,
    );
  });
}

export async function imageBitmapToJpeg(bitmap: ImageBitmap, quality = 0.9): Promise<Blob> {
  return drawableToJpeg(bitmap, bitmap.width, bitmap.height, quality);
}

export async function videoElementToJpeg(
  el: HTMLVideoElement,
  quality = 0.9,
): Promise<Blob | null> {
  const { videoWidth, videoHeight } = el;
  if (!videoWidth || !videoHeight) return null;
  return drawableToJpeg(el, videoWidth, videoHeight, quality);
}
