export interface FrameObjectRef {
  id: string;
  x: number;
  y: number;
}

export interface FrameCategories {
  ai: string[];
  user: string[];
  track: string[];
}

const CATEGORY_ORDER: (keyof FrameCategories)[] = ["ai", "user", "track"];

function spatialSort(items: readonly FrameObjectRef[]): string[] {
  return [...items]
    .sort((a, b) => a.y - b.y || a.x - b.x || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((o) => o.id);
}

export function buildFrameCategories(
  ai: readonly FrameObjectRef[],
  user: readonly FrameObjectRef[],
  track: readonly FrameObjectRef[],
): FrameCategories {
  return { ai: spatialSort(ai), user: spatialSort(user), track: spatialSort(track) };
}

export interface FrameObjectSources {
  ai: readonly FrameObjectRef[];
  entries: readonly (FrameObjectRef & { isTrack: boolean })[];
  carryOverGhosts: readonly FrameObjectRef[];
  selectedTrackGhost: FrameObjectRef | null;
}

export function collectFrameCategories(src: FrameObjectSources): FrameCategories {
  const user: FrameObjectRef[] = [];
  const track: FrameObjectRef[] = [];
  for (const e of src.entries) {
    const ref: FrameObjectRef = { id: e.id, x: e.x, y: e.y };
    (e.isTrack ? track : user).push(ref);
  }
  for (const g of src.carryOverGhosts) track.push(g);
  if (src.selectedTrackGhost) track.push(src.selectedTrackGhost);
  return buildFrameCategories(src.ai, user, track);
}

function categoryOf(cats: FrameCategories, id: string | null): keyof FrameCategories | null {
  if (!id) return null;
  for (const key of CATEGORY_ORDER) {
    if (cats[key].includes(id)) return key;
  }
  return null;
}

function firstOfFirstNonEmpty(cats: FrameCategories): string | null {
  for (const key of CATEGORY_ORDER) {
    if (cats[key].length > 0) return cats[key][0];
  }
  return null;
}

export function nextInCategory(
  cats: FrameCategories,
  selectedId: string | null,
  dir: -1 | 1,
): string | null {
  const key = categoryOf(cats, selectedId);
  if (!key) return firstOfFirstNonEmpty(cats);
  const list = cats[key];
  const idx = list.indexOf(selectedId!);
  return list[(idx + dir + list.length) % list.length];
}

export function nextCategory(
  cats: FrameCategories,
  selectedId: string | null,
  dir: -1 | 1,
): string | null {
  const nonEmpty = CATEGORY_ORDER.filter((k) => cats[k].length > 0);
  if (nonEmpty.length === 0) return null;
  const curKey = categoryOf(cats, selectedId);
  const curIdx = curKey ? nonEmpty.indexOf(curKey) : -1;
  if (curIdx < 0) {
    const start = dir > 0 ? nonEmpty[0] : nonEmpty[nonEmpty.length - 1];
    return cats[start][0];
  }
  const nextKey = nonEmpty[(curIdx + dir + nonEmpty.length) % nonEmpty.length];
  return cats[nextKey][0];
}
