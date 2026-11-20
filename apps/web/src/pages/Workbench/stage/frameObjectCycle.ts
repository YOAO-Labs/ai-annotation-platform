// 当前帧「对象环」两级循环的纯逻辑(与渲染解耦, 便于单测)。
//
// 三类对象: AI 待审 / 人工 video_bbox / 轨迹当前帧视图。各类内按空间位置稳定排序
// (y↑ 优先、x↑ 次之、id tie-break), 保证同帧同集合每次同序、"下一个"可预期。
//
// - Tab / Shift+Tab → nextInCategory: 按选中对象所属类, 环内 next/prev。
// - `  / Shift+`    → nextCategory:  跳下一/上一非空类首对象。

export interface FrameObjectRef {
  id: string;
  /** 归一化左上角坐标, 仅用于稳定排序。 */
  x: number;
  y: number;
}

export interface FrameCategories {
  ai: string[];
  user: string[];
  track: string[];
}

/** 跨类跳转的类顺序: AI 待审 → 人工 → 轨迹。 */
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

/** 当前帧对象来源(与渲染视图一一对应, 便于把「哪些进 Tab 环」的组装逻辑纯化 + 单测)。 */
export interface FrameObjectSources {
  /** AI 待审候选(扁平 x/y)。 */
  ai: readonly FrameObjectRef[];
  /** 当前帧已画实框(人工 video_bbox + 轨迹解析帧), isTrack 区分归「轨迹」还是「人工」类。 */
  entries: readonly (FrameObjectRef & { isTrack: boolean })[];
  /** 跨网格帧续写的「其余待续轨迹」参考虚影(carryOverGhosts, 已排除选中那条)。 */
  carryOverGhosts: readonly FrameObjectRef[];
  /**
   * 选中轨迹当前帧无实框时的参考虚影(ghost)。**必须并入「轨迹」类**——否则选中它时
   * `categoryOf` 认不出所属类, `nextInCategory` 退化成 `firstOfFirstNonEmpty`(恒返首元素、
   * 忽略方向), Tab 只会在「当前选中」与「空间首条」之间来回弹, 到不了其余待续轨迹。
   */
  selectedTrackGhost: FrameObjectRef | null;
}

/** 把当前帧各来源归并成三类对象环(轨迹类含选中 ghost, 保证 Tab 能覆盖全部待续轨迹)。 */
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

/** selectedId 命中的类; 无命中(未选/不在当前帧任何类)返回 null。 */
function categoryOf(cats: FrameCategories, id: string | null): keyof FrameCategories | null {
  if (!id) return null;
  for (const key of CATEGORY_ORDER) {
    if (cats[key].includes(id)) return key;
  }
  return null;
}

/** 第一个非空类的首对象; 全空返回 null。 */
function firstOfFirstNonEmpty(cats: FrameCategories): string | null {
  for (const key of CATEGORY_ORDER) {
    if (cats[key].length > 0) return cats[key][0];
  }
  return null;
}

/**
 * 同类流转: 选中对象所属类内环形 next/prev。
 * 无选中或选中不在当前帧任何类 → 落到第一个非空类首对象。全空返回 null。
 */
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

/**
 * 跨类跳转: 跳到下一/上一非空类的首对象(类顺序 AI→人工→轨迹, 环形, 跳过空类)。
 * 无选中 → dir>0 落第一个非空类、dir<0 落最后一个非空类。全空返回 null。
 */
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
