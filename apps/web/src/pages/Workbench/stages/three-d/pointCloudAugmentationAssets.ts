import type {
  PointCloudAugmentationAsset,
  PointCloudAugmentationCamera,
  PointCloudAugmentationManifest,
} from "@/api/pointCloudAugmentation";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import { decodeAuxiliaryPointCloudFrameAsync } from "./geometry/pointCloudComputeSession";
import type { DecodedAuxiliaryPointCloudFrame } from "./geometry/pointcloudFrame";

export const AUXILIARY_CACHE_MAX_BYTES = 32 * 1024 * 1024;
const AUXILIARY_CACHE_MAX_ENTRIES = 3;

/** Separate completed-content cache; it cannot evict measured-frame or camera assets. */
export class PointCloudAugmentationCache {
  private readonly entries = new Map<string, DecodedAuxiliaryPointCloudFrame>();
  get(key: string) {
    const entry = this.entries.get(key);
    if (entry) {
      this.entries.delete(key);
      this.entries.set(key, entry);
    }
    return entry;
  }
  put(key: string, frame: DecodedAuxiliaryPointCloudFrame) {
    this.entries.delete(key);
    if (frame.positions.byteLength + frame.colors.byteLength > AUXILIARY_CACHE_MAX_BYTES) return;
    this.entries.set(key, frame);
    while (
      this.entries.size > AUXILIARY_CACHE_MAX_ENTRIES ||
      this.bytes > AUXILIARY_CACHE_MAX_BYTES
    ) {
      const first = this.entries.keys().next().value;
      if (first === undefined) break;
      this.entries.delete(first);
    }
  }
  clear() {
    this.entries.clear();
  }
  get size() {
    return this.entries.size;
  }
  get bytes() {
    return [...this.entries.values()].reduce(
      (total, frame) => total + frame.positions.byteLength + frame.colors.byteLength,
      0,
    );
  }
}

export function augmentationContentKey(
  userId: string,
  manifest: PointCloudAugmentationManifest,
  camera: PointCloudAugmentationCamera,
  budget: number,
): string {
  return JSON.stringify([
    userId,
    manifest.task_id,
    manifest.asset_id,
    manifest.generation,
    manifest.input_digest,
    camera.role,
    camera.sha256,
    manifest.coordinate_frame,
    manifest.axis_convention,
    budget,
  ]);
}

export function validateAugmentationAsset(
  asset: PointCloudAugmentationAsset,
  manifest: PointCloudAugmentationManifest,
  camera: PointCloudAugmentationCamera,
): void {
  if (
    asset.generation !== manifest.generation ||
    asset.camera_role !== camera.role ||
    asset.coordinate_frame !== "primary_pointcloud" ||
    asset.axis_convention !== manifest.axis_convention ||
    asset.format !== "pcd" ||
    asset.sha256 !== camera.sha256 ||
    asset.point_count !== camera.point_count ||
    !Number.isFinite(asset.expires_in) ||
    asset.expires_in <= 0 ||
    asset.expires_in > 300
  ) {
    throw new Error("辅助点云资产身份不一致，请重新准备结果");
  }
}

/** Refresh an expired signed URL once, and verify content before handing it to the shared worker. */
export async function loadAugmentationAsset(
  manifest: PointCloudAugmentationManifest,
  camera: PointCloudAugmentationCamera,
  convention: LidarAxisConvention,
  budget: number,
  issueUrl: () => Promise<PointCloudAugmentationAsset>,
  signal: AbortSignal,
): Promise<DecodedAuxiliaryPointCloudFrame> {
  if (
    manifest.coordinate_frame !== "primary_pointcloud" ||
    manifest.axis_convention !== convention
  ) {
    throw new Error("辅助点云坐标系与当前实测点云不一致");
  }
  if (
    !Number.isInteger(camera.byte_size) ||
    camera.byte_size < 1 ||
    camera.byte_size > AUXILIARY_CACHE_MAX_BYTES ||
    !Number.isInteger(camera.point_count) ||
    camera.point_count < 0 ||
    !/^[0-9a-f]{64}$/.test(camera.sha256)
  ) {
    throw new Error("辅助点云资源声明无效或超过加载预算");
  }
  let response: Response | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    const asset = await issueUrl();
    if (signal.aborted) throw new DOMException("Auxiliary owner retired", "AbortError");
    validateAugmentationAsset(asset, manifest, camera);
    response = await fetch(asset.url, { signal, cache: "no-store" });
    if (response.ok || (response.status !== 401 && response.status !== 403)) break;
    await response.body?.cancel();
  }
  if (!response?.ok) throw new Error(`辅助点云加载失败 (${response?.status ?? "network"})`);
  const length = response.headers.get("content-length");
  if (length !== null && Number(length) !== camera.byte_size) {
    await response.body?.cancel();
    throw new Error("辅助点云资源长度不一致");
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("浏览器无法读取辅助点云资源");
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > camera.byte_size || size > AUXILIARY_CACHE_MAX_BYTES)
        throw new Error("辅助点云资源超过声明大小");
      chunks.push(chunk.value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
  if (size !== camera.byte_size) throw new Error("辅助点云资源不完整");
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器无法校验辅助点云完整性");
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  const sha256 = [...hash].map((value) => value.toString(16).padStart(2, "0")).join("");
  if (sha256 !== camera.sha256) throw new Error("辅助点云完整性校验失败");
  if (signal.aborted) throw new DOMException("Auxiliary owner retired", "AbortError");
  const frame = await decodeAuxiliaryPointCloudFrameAsync(bytes.buffer, convention, budget, signal);
  if (frame.totalPoints !== camera.point_count) throw new Error("辅助点云点数与资产声明不一致");
  return frame;
}
