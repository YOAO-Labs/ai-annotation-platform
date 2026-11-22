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

export function getApiP95Ms(): number | null {
  const cutoff = Date.now() - WINDOW_MS;
  while (buffer.length > 0 && buffer[0].t < cutoff) buffer.shift();
  if (buffer.length === 0) return null;
  const sorted = buffer.map((e) => e.d).sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
  return Math.round(sorted[idx]);
}

export function _resetApiMetrics(): void {
  buffer.length = 0;
}
