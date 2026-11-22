type Vec3 = readonly [number, number, number];

export interface PerspectiveBoxFramingInput {
  boxCenter: Vec3;
  boxSize: Vec3;
  cameraPosition: Vec3;
  cameraTarget: Vec3;
  fallbackDirection: Vec3;
  verticalFovDeg: number;
  aspect: number;
  fullFrameFar: number;
  marginScale?: number;
}

export interface PerspectiveBoxFraming {
  position: [number, number, number];
  target: [number, number, number];
  near: number;
  far: number;
  distance: number;
  radius: number;
}

const DEFAULT_MARGIN_SCALE = 1.2;
const MIN_NEAR = 0.01;
const MIN_DIRECTION_LENGTH = 1e-9;

function normalizedDirection(primary: Vec3, fallback: Vec3): [number, number, number] {
  const normalize = (value: Vec3): [number, number, number] | null => {
    if (value.some((part) => !Number.isFinite(part))) return null;
    const length = Math.hypot(value[0], value[1], value[2]);
    if (length < MIN_DIRECTION_LENGTH) return null;
    return [value[0] / length, value[1] / length, value[2] / length];
  };
  return normalize(primary) ?? normalize(fallback) ?? [0, -Math.SQRT1_2, Math.SQRT1_2];
}

export function framePerspectiveBox(input: PerspectiveBoxFramingInput): PerspectiveBoxFraming {
  const marginScale =
    Number.isFinite(input.marginScale) && (input.marginScale ?? 0) > 0
      ? input.marginScale!
      : DEFAULT_MARGIN_SCALE;
  const radius = Math.max(
    Math.hypot(Math.abs(input.boxSize[0]), Math.abs(input.boxSize[1]), Math.abs(input.boxSize[2])) /
      2,
    MIN_NEAR,
  );
  const paddedRadius = radius * marginScale;
  const verticalFov = (Math.min(179, Math.max(1, input.verticalFovDeg)) * Math.PI) / 180;
  const aspect = Number.isFinite(input.aspect) && input.aspect > 0 ? input.aspect : 1;
  const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * aspect);
  const limitingHalfFov = Math.min(verticalFov, horizontalFov) / 2;
  const distance = paddedRadius / Math.sin(limitingHalfFov);
  const direction = normalizedDirection(
    [
      input.cameraPosition[0] - input.cameraTarget[0],
      input.cameraPosition[1] - input.cameraTarget[1],
      input.cameraPosition[2] - input.cameraTarget[2],
    ],
    input.fallbackDirection,
  );
  const target: [number, number, number] = [
    input.boxCenter[0],
    input.boxCenter[1],
    input.boxCenter[2],
  ];
  const position: [number, number, number] = [
    target[0] + direction[0] * distance,
    target[1] + direction[1] * distance,
    target[2] + direction[2] * distance,
  ];
  const near = Math.max(MIN_NEAR, distance - paddedRadius * 1.05);
  const focusFar = distance + paddedRadius * 1.05;
  const fullFrameFar = Number.isFinite(input.fullFrameFar) ? input.fullFrameFar : 0;

  return {
    position,
    target,
    near,
    far: Math.max(fullFrameFar, focusFar, near * 10),
    distance,
    radius,
  };
}
