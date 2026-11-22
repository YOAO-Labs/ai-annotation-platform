import type { ClassesConfig } from "@/api/projects";

let _activeConfig: ClassesConfig | undefined;
export function setActiveClassesConfig(c: ClassesConfig | undefined): void {
  _activeConfig = c;
}

export const CLASS_COLORS: Record<string, string> = {
  商品: "oklch(0.62 0.18 252)",
  价签: "oklch(0.65 0.18 152)",
  标识牌: "oklch(0.68 0.16 75)",
  缺货位: "oklch(0.62 0.20 25)",
  促销贴: "oklch(0.60 0.20 295)",
};

export const UNKNOWN_CLASS = "__unknown";
export const UNKNOWN_CLASS_LABEL = "未分类";
const UNKNOWN_COLOR = "oklch(0.65 0 0)";

export function displayClassName(name: string): string {
  return name === UNKNOWN_CLASS ? UNKNOWN_CLASS_LABEL : name;
}

const _canvasCache = new Map<string, string>();

function directColorToHex(cssColor: string): string | null {
  const raw = cssColor.trim();
  const shortHex = raw.match(/^#([0-9a-fA-F]{3})$/);
  if (shortHex) {
    const [r, g, b] = shortHex[1].split("");
    return `#${r}${r}${g}${g}${b}${b}`.toLowerCase();
  }
  const longHex = raw.match(/^#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$/);
  if (longHex) return `#${longHex[1]}`.toLowerCase();

  const rgb = raw.match(
    /^rgba?\(\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)/i,
  );
  if (!rgb) return null;
  const toByte = (value: string) => Math.max(0, Math.min(255, Math.round(Number(value))));
  const [r, g, b] = [toByte(rgb[1]), toByte(rgb[2]), toByte(rgb[3])];
  return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
}

function hasCanvasColorParser(): boolean {
  return (
    typeof document !== "undefined" &&
    !(typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent))
  );
}

export function colorToHex(cssColor: string): string {
  if (_canvasCache.has(cssColor)) return _canvasCache.get(cssColor)!;
  const direct = directColorToHex(cssColor);
  if (direct) {
    _canvasCache.set(cssColor, direct);
    return direct;
  }
  if (!hasCanvasColorParser()) return "#888888";
  try {
    const cvs = document.createElement("canvas");
    cvs.width = cvs.height = 1;
    const ctx = cvs.getContext("2d");
    if (!ctx) return "#888888";
    ctx.fillStyle = cssColor;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    const hex = `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
    _canvasCache.set(cssColor, hex);
    return hex;
  } catch {
    return "#888888";
  }
}

export function classColorForCanvas(name: string, config?: ClassesConfig): string {
  return colorToHex(classColor(name, config));
}

export function hexToRgb(hex: string): readonly [number, number, number] {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

export function cssVarToHex(varName: string, _themeKey?: string): string {
  if (typeof document === "undefined") return "#888888";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return raw ? colorToHex(raw) : "#888888";
}

export function hexToRgba(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r},${g},${b},${alpha})`;
}

function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (h * 16777619) >>> 0;
  }
  return h;
}

function mixHash(h: number): number {
  h ^= h >>> 16;
  h = Math.imul(h, 0x7feb352d);
  h ^= h >>> 15;
  h = Math.imul(h, 0x846ca68b);
  h ^= h >>> 16;
  return h >>> 0;
}

export function classColor(name: string, config?: ClassesConfig): string {
  if (name === UNKNOWN_CLASS) return UNKNOWN_COLOR;
  const cfg = (config ?? _activeConfig)?.[name];
  if (cfg?.color) return cfg.color;
  if (CLASS_COLORS[name]) return CLASS_COLORS[name];
  const hue = hashString(name) % 360;
  return `oklch(0.62 0.18 ${hue})`;
}

export const TRACK_COLOR_PALETTE: { label: string; value: string }[] = [
  { label: "蓝", value: "oklch(0.62 0.18 252)" },
  { label: "绿", value: "oklch(0.65 0.18 152)" },
  { label: "橙", value: "oklch(0.68 0.16 75)" },
  { label: "红", value: "oklch(0.62 0.20 25)" },
  { label: "紫", value: "oklch(0.60 0.20 295)" },
  { label: "青", value: "oklch(0.64 0.14 200)" },
  { label: "粉", value: "oklch(0.68 0.18 350)" },
  { label: "灰", value: "oklch(0.65 0 0)" },
];

const TRACK_LIGHTNESS_BANDS = [0.56, 0.62, 0.68, 0.74] as const;
const TRACK_CHROMA_BANDS = [0.23, 0.2, 0.17, 0.14] as const;

export function defaultTrackColor(trackId: string, className: string): string {
  if (!trackId) return classColor(className);
  const hash = mixHash(hashString(`track:${trackId}`));
  const hue = ((hash & 0xffff) / 0x10000) * 360;
  const lightness = TRACK_LIGHTNESS_BANDS[(hash >>> 16) % TRACK_LIGHTNESS_BANDS.length];
  const chroma = TRACK_CHROMA_BANDS[(hash >>> 19) % TRACK_CHROMA_BANDS.length];
  return `oklch(${lightness.toFixed(2)} ${chroma.toFixed(2)} ${hue.toFixed(2)})`;
}

export function getTrackColor(
  trackId: string,
  className: string,
  overrides?: Record<string, string>,
): string {
  const override = overrides?.[trackId];
  if (override) return override;
  return defaultTrackColor(trackId, className);
}

export function sortClassesByConfig(classes: string[], config?: ClassesConfig): string[] {
  if (!config) return classes;
  return classes.slice().sort((a, b) => {
    const oa = config[a]?.order ?? Number.POSITIVE_INFINITY;
    const ob = config[b]?.order ?? Number.POSITIVE_INFINITY;
    if (oa !== ob) return oa - ob;
    return classes.indexOf(a) - classes.indexOf(b);
  });
}
