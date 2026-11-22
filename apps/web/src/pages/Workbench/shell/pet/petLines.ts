export type PetMood =
  | "idle"
  | "idleTalk"
  | "holding"
  | "selected"
  | "multiSelected"
  | "aiRunning"
  | "candidateReady"
  | "warning"
  | "offline"
  | "review"
  | "happy"
  | "celebrate";

export const MILESTONES = [10, 25, 50, 100, 200, 500, 1000] as const;

type LineKey = "idleTalk" | "poke" | "celebrate";

const LINES: Record<LineKey, readonly string[]> = {
  idleTalk: [
    "可以休息一下眼睛",
    "要不要伸个懒腰?",
    "这张图先慢慢看",
    "喝口水再继续",
    "我在这儿,慢慢来",
    "盯久了记得眨眨眼",
  ],
  poke: ["在的", "我守着当前题", "继续看画布", "需要时点我展开"],
  celebrate: ["里程碑达成", "进度不错", "又上一个台阶"],
};

export function pickLine(key: LineKey, count?: number): string {
  if (key === "celebrate" && typeof count === "number") {
    return `${count} 个达成`;
  }
  const pool = LINES[key];
  return pool[Math.floor(Math.random() * pool.length)];
}
