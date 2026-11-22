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
