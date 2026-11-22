import type { BboxGeometry, Geometry } from "@/types";

export function isComplexPolygonGeometry(geometry: Geometry | null | undefined): boolean {
  if (!geometry) return false;
  if (geometry.type === "multi_polygon") return true;
  return geometry.type === "polygon" && (geometry.holes?.length ?? 0) > 0;
}

export function supportsSingleRingPolygonEdit(geometry: Geometry | null | undefined): boolean {
  return geometry?.type === "polygon" && !isComplexPolygonGeometry(geometry);
}

export function supportsBBoxNudge(geometry: Geometry | null | undefined): geometry is BboxGeometry {
  return geometry?.type === "bbox";
}
