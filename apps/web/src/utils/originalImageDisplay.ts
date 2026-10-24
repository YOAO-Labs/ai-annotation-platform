/** Interpret verified bytes for display without changing the original content. */
async function readBlob(blob: Blob, signal?: AbortSignal): Promise<ArrayBuffer> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => reader.abort();
    const cleanup = () => signal?.removeEventListener("abort", abort);
    reader.onload = () => {
      cleanup();
      if (signal?.aborted) reject(new DOMException("Image read aborted", "AbortError"));
      else resolve(reader.result as ArrayBuffer);
    };
    reader.onerror = () => {
      cleanup();
      reject(new Error("无法读取原件图片，请重试"));
    };
    reader.onabort = () => {
      cleanup();
      reject(new DOMException("Image read aborted", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    reader.readAsArrayBuffer(blob);
  });
}

export async function typedOriginalImageBlob(blob: Blob, signal?: AbortSignal): Promise<Blob> {
  const bytes = new Uint8Array(await readBlob(blob.slice(0, 128 * 1024), signal));
  signal?.throwIfAborted();
  const starts = (...prefix: number[]) => prefix.every((value, index) => bytes[index] === value);
  const ascii = (start: number, end: number) => String.fromCharCode(...bytes.slice(start, end));
  let type: string | undefined;
  if (starts(137, 80, 78, 71, 13, 10, 26, 10)) type = "image/png";
  else if (starts(255, 216, 255)) type = "image/jpeg";
  else if (["GIF87a", "GIF89a"].includes(ascii(0, 6))) type = "image/gif";
  else if (ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP") type = "image/webp";
  else if (ascii(0, 2) === "BM") type = "image/bmp";
  else if (
    starts(73, 73, 42, 0) ||
    starts(77, 77, 0, 42) ||
    starts(73, 73, 43, 0) ||
    starts(77, 77, 0, 43)
  )
    type = "image/tiff";
  else {
    const encoding = starts(255, 254) ? "utf-16le" : starts(254, 255) ? "utf-16be" : "utf-8";
    const text = new TextDecoder(encoding).decode(bytes).replace(/^\uFEFF/, "");
    const root = text
      .replace(/^\s*(?:<\?xml[\s\S]*?\?>\s*)?/, "")
      .replace(/^(?:<!--[\s\S]*?-->\s*)*/, "")
      .replace(/^<!DOCTYPE\s+svg\b(?:[^>[]|\[[\s\S]*?\])*>\s*/i, "");
    if (/^<svg(?:\s|>)/i.test(root)) type = "image/svg+xml";
  }
  if (!type) throw new Error("无法识别原件图片格式；可以下载已核验原件");
  return blob.slice(0, blob.size, type);
}

export async function createOriginalImageAddress(blob: Blob, signal?: AbortSignal) {
  const typed = await typedOriginalImageBlob(blob, signal);
  signal?.throwIfAborted();
  if (typed.type === "image/svg+xml") {
    // A data URL keeps SVG documents opaque even if opened outside the image element.
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      const abort = () => reader.abort();
      const cleanup = () => signal?.removeEventListener("abort", abort);
      reader.onload = () => {
        cleanup();
        if (signal?.aborted || typeof reader.result !== "string")
          reject(new DOMException("Image read aborted", "AbortError"));
        else resolve(reader.result);
      };
      reader.onerror = () => {
        cleanup();
        reject(new Error("无法读取 SVG 原件，请重试"));
      };
      reader.onabort = () => {
        cleanup();
        reject(new DOMException("Image read aborted", "AbortError"));
      };
      signal?.addEventListener("abort", abort, { once: true });
      reader.readAsDataURL(typed);
    });
    signal?.throwIfAborted();
    return { url, release() {} };
  }
  const url = URL.createObjectURL(typed);
  let released = false;
  return {
    url,
    release() {
      if (!released) URL.revokeObjectURL(url);
      released = true;
    },
  };
}
