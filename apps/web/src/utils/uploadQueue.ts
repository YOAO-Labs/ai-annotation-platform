export type QueueItemStatus = "pending" | "running" | "done" | "error";

export interface QueueItem<T = unknown> {
  id: string;
  status: QueueItemStatus;
  progress: number;
  error?: string;
  result?: T;
}

export type QueueWorker<T> = (
  signal: { aborted: boolean },
  onProgress: (pct: number) => void,
) => Promise<T>;

interface Task<T> {
  id: string;
  worker: QueueWorker<T>;
}

interface RunOptions {
  concurrency?: number;
  onUpdate?: () => void;
}

export async function runUploadQueue<T>(
  tasks: Task<T>[],
  items: Map<string, QueueItem<T>>,
  opts: RunOptions = {},
): Promise<void> {
  const concurrency = Math.max(1, opts.concurrency ?? 3);
  const notify = () => opts.onUpdate?.();
  let cursor = 0;

  const runOne = async (): Promise<void> => {
    while (cursor < tasks.length) {
      const idx = cursor++;
      const task = tasks[idx];
      const it = items.get(task.id);
      if (!it) continue;
      it.status = "running";
      it.progress = 0;
      notify();
      try {
        const signal = { aborted: false };
        const result = await task.worker(signal, (pct) => {
          it.progress = Math.max(0, Math.min(100, pct));
          notify();
        });
        it.status = "done";
        it.progress = 100;
        it.result = result;
      } catch (err) {
        it.status = "error";
        it.error = err instanceof Error ? err.message : String(err);
      }
      notify();
    }
  };

  await Promise.all(Array.from({ length: concurrency }, () => runOne()));
}

export function putWithProgress(
  url: string,
  file: File,
  onProgress: (pct: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    if (file.type) xhr.setRequestHeader("Content-Type", file.type);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress((e.loaded / e.total) * 100);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`上传失败 (HTTP ${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("网络错误，请检查 MinIO/CORS 配置"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.send(file);
  });
}
