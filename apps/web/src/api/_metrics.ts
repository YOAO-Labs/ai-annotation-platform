// API duration ring buffer 用于 PerfHud 浏览器侧 p95 指标.
// 简单实现: 60s 滑动窗口, 每条记录 { t: 完成时间戳 ms, d: 耗时 ms }.
// 老条目按访问时(getApiP95)惰性过滤; 内存上限 1024 条防爆.

const WINDOW_MS = 60_000;
const MAX_ENTRIES = 1024;

interface Entry {
  t: number;
  d: number;
}

const buffer: Entry[] = [];

export function recordApiDuration(ms: number): void {
  if (!Number.isFinite(ms) || ms < 0) return;
  buffer.push({ t: Date.now(), d: ms });
  if (buffer.length > MAX_ENTRIES) {
    buffer.splice(0, buffer.length - MAX_ENTRIES);
  }
}

/** 返回最近 60s 内 API 耗时的 p95 (ms); 无数据返回 null. */
export function getApiP95Ms(): number | null {
  const cutoff = Date.now() - WINDOW_MS;
  // 惰性清理过期条目
  while (buffer.length > 0 && buffer[0].t < cutoff) buffer.shift();
  if (buffer.length === 0) return null;
  const sorted = buffer.map((e) => e.d).sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
  return Math.round(sorted[idx]);
}

/** 测试用: 清空 buffer. */
export function _resetApiMetrics(): void {
  buffer.length = 0;
}
