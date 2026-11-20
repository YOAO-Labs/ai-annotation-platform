/**
 * 相机 → 悬浮锚点推导(L0,SUSTech 式朝向环绕布局)。
 *
 * 把每个相机按物理朝向钉到主视图四周的某个边缘 / 角落,使「空间位置 = 物理朝向」,
 * 省掉看相机时的脑内方位映射。返回 9 种锚点之一(8 方位 + overflow 兜底)。
 *
 * 推导口径(**名字优先,外参兜底**):
 *   1. 名字优先:相机 role/name 里的 front/rear/left/right(及 front_left 等复合)直接编码
 *      标注意图,最稳。复合(front_left)先于简单(front/left)匹配。
 *   2. 外参兜底:名字认不出时,取相机光轴前向向量在 world 水平面的方位角推方位。
 *      **此分支假设 lidar 系为标准约定 X=前 / Y=左 / Z=上**(KITTI/ROS REP-103)。
 *      注:示例集 pc-scene-a 的 lidar 系前向其实是 -Y(非标准),故那份数据必然走名字分支;
 *      外参分支只为「无可识别名字」的相机(如 CAM_0)兜底,标准约定是此时的最佳猜测。
 *   3. 都认不出 / 外参退化(光轴近垂直)→ overflow(落底部小条,不丢相机)。
 */
import type { SensorCalibration } from "@/types";

export type Anchor =
  | "top"
  | "bottom"
  | "left"
  | "right"
  | "top-left"
  | "top-right"
  | "bottom-left"
  | "bottom-right"
  | "overflow";

// 复合在前、简单在后(front_left 必须先于 front/left 命中)。
const NAME_RULES: ReadonlyArray<readonly [RegExp, Anchor]> = [
  [/front[_-]?left|left[_-]?front/, "top-left"],
  [/front[_-]?right|right[_-]?front/, "top-right"],
  [/(rear|back)[_-]?left|left[_-]?(rear|back)/, "bottom-left"],
  [/(rear|back)[_-]?right|right[_-]?(rear|back)/, "bottom-right"],
  [/front|forward/, "top"],
  [/rear|back/, "bottom"],
  [/left/, "left"],
  [/right/, "right"],
];

function anchorByName(idOrName: string): Anchor | null {
  const s = idOrName.toLowerCase();
  for (const [re, anchor] of NAME_RULES) {
    if (re.test(s)) return anchor;
  }
  return null;
}

/** 方位角(度,标准系 X=前/Y=左)→ 锚点。+X→top,+Y→left,-X→bottom,-Y→right。 */
function azimuthToAnchor(azDeg: number): Anchor {
  let a = azDeg;
  while (a <= -180) a += 360;
  while (a > 180) a -= 360;
  const abs = Math.abs(a);
  if (abs <= 22.5) return "top"; // +X 前
  if (abs >= 157.5) return "bottom"; // -X 后
  if (a > 22.5 && a < 67.5) return "top-left";
  if (a >= 67.5 && a <= 112.5) return "left"; // +Y 左
  if (a > 112.5 && a < 157.5) return "bottom-left";
  if (a < -22.5 && a > -67.5) return "top-right";
  if (a <= -67.5 && a >= -112.5) return "right"; // -Y 右
  return "bottom-right"; // (-157.5, -112.5)
}

function anchorByExtrinsic(calib: SensorCalibration | null | undefined): Anchor | null {
  const e = calib?.extrinsic;
  if (!e || e.length < 11) return null;
  // 光轴前向在 world = extrinsic 第三行 (e8,e9,e10)(world→camera 旋转的转置取 +Z 列)。
  const fx = e[8];
  const fy = e[9];
  if (!isFinite(fx) || !isFinite(fy)) return null;
  if (Math.hypot(fx, fy) < 1e-3) return null; // 光轴近垂直 → 水平方位退化,交给 overflow
  return azimuthToAnchor((Math.atan2(fy, fx) * 180) / Math.PI);
}

/**
 * 推一个相机的悬浮锚点。idOrName 传 role 优先(更 canonical),回退显示 name。
 * calib 仅在名字认不出时用于外参兜底。
 */
export function cameraAnchor(
  calib: SensorCalibration | null | undefined,
  idOrName: string,
): Anchor {
  return anchorByName(idOrName) ?? anchorByExtrinsic(calib) ?? "overflow";
}
