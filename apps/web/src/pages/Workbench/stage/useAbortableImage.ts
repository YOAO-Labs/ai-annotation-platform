import { useLayoutEffect, useState } from "react";

export type AbortableImageStatus = "loading" | "loaded" | "failed";

export function loadAbortableImage(url: string, signal: AbortSignal): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const cleanup = () => {
      image.removeEventListener("load", onLoad);
      image.removeEventListener("error", onError);
      signal.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      image.src = "";
      reject(new DOMException("Image load aborted", "AbortError"));
    };
    const onError = () => {
      cleanup();
      reject(new Error(`Image load failed: ${url}`));
    };
    const onLoad = () => {
      void image
        .decode()
        .catch(() => undefined)
        .then(() => {
          if (signal.aborted) return;
          cleanup();
          resolve(image);
        });
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    image.addEventListener("load", onLoad);
    image.addEventListener("error", onError);
    signal.addEventListener("abort", onAbort, { once: true });
    image.src = url;
  });
}

export function useAbortableImage(
  url: string,
  retryKey: number = 0,
): [HTMLImageElement | undefined, AbortableImageStatus] {
  const [state, setState] = useState<{
    url: string;
    retryKey: number;
    image: HTMLImageElement | undefined;
    status: AbortableImageStatus;
  }>({ url: "", retryKey, image: undefined, status: "loading" });

  useLayoutEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    void loadAbortableImage(url, controller.signal).then(
      (image) => setState({ url, retryKey, image, status: "loaded" }),
      () => {
        if (!controller.signal.aborted) {
          setState({ url, retryKey, image: undefined, status: "failed" });
        }
      },
    );
    return () => controller.abort();
  }, [retryKey, url]);

  if (state.url !== url || state.retryKey !== retryKey) return [undefined, "loading"];
  return [state.image, state.status];
}
