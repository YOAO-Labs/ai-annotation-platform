/**
 * 当前视频帧 → JPEG Blob。
 *
 * 视频工作台把当前帧解码成 ImageBitmap 缓存(useVideoBitmapCache);单题 AI 与交互式 SAM
 * 需把它编码成 JPEG 随 multipart 传后端(client 供图路径)。优先 OffscreenCanvas.convertToBlob;
 * 缺失时(旧浏览器)回落 <canvas>.toBlob。
 *
 * 位图缓存未命中时,画布渲染的其实是 <video> 元素本身(见 pickMediaImageSource),
 * 故取帧也提供 videoElementToJpeg 走同一回退,避免「画面看得见却报帧未就绪」。
 */
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

/**
 * <video> 元素当前显示帧 → JPEG。
 * 元数据未就绪(videoWidth 为 0)时返回 null,由调用方判定「帧确实还没到」。
 */
export async function videoElementToJpeg(
  el: HTMLVideoElement,
  quality = 0.9,
): Promise<Blob | null> {
  const { videoWidth, videoHeight } = el;
  if (!videoWidth || !videoHeight) return null;
  return drawableToJpeg(el, videoWidth, videoHeight, quality);
}
