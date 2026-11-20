/**
 * 点云地面高度估计(纯函数,jsdom 可单测)。
 *
 * 用 z 的 1% 分位能抵抗少量离群低点，但在车顶 LiDAR 数据集上偏低；
 * 零星远处雷达回波或反射噪声会把分位拉到真地面以下。
 * 放置新框回退到地面平面时，过低的估值会把框放到地面以下。
 *
 * 改用 z **中位数(50% 分位)**:对低端噪声极度鲁棒,只要"地面 + 物体"两类点云大致分布
 * 在 z 上半 / 下半即可工作;车顶 lidar 缺自车下方点云时,中位数大致落在车体中部高度,
 * PointCloudScene.placeOnGround 优先让射线命中点云；地面平面 fallback
 * 仅在点击空地时触发,中位数偏高也比 1% 分位偏低更安全(框漂浮 < 框埋地下,
 * 都能用 Q 一键贴合纠正)。
 *
 * 实现:128 bin 直方图(避免排序 O(N log N)),累计到 count*0.5 的 bin 中心。
 */

/** 估计点云地面高度(米)。空输入返回 0。同高度集合返回该高度。 */
export function estimateGroundZ(positions: Float32Array, count: number): number {
  if (count === 0) return 0;
  let zMin = Infinity;
  let zMax = -Infinity;
  for (let i = 0; i < count; i++) {
    const z = positions[i * 3 + 2];
    if (z < zMin) zMin = z;
    if (z > zMax) zMax = z;
  }
  const span = zMax - zMin;
  if (span <= 0) return zMin;
  const BINS = 128;
  const hist = new Int32Array(BINS);
  for (let i = 0; i < count; i++) {
    const t = (positions[i * 3 + 2] - zMin) / span;
    const b = Math.min(BINS - 1, Math.floor(t * BINS));
    hist[b]++;
  }
  const target = count * 0.5;
  let acc = 0;
  for (let b = 0; b < BINS; b++) {
    acc += hist[b];
    if (acc >= target) return zMin + ((b + 0.5) / BINS) * span;
  }
  return zMin;
}
