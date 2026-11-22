import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import type { CocoRleMaskRef } from "@/types";
import type { MeResponse } from "./auth";
import { rasterMasksApi } from "./rasterMasks";

const annotation = { task_id: "task-1", id: "annotation-1", version: 1 };
const rle = { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 3] };
const reference: CocoRleMaskRef = {
  encoding: "coco_rle_ref",
  object_key: "mask-1",
  sha256: "a".repeat(64),
  size: [2, 3],
  runs: 3,
  bytes: 64,
};

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function maskResponse(sha256: string) {
  return Response.json(rle, { headers: { ETag: `"${sha256}"` } });
}

describe("annotation video Mask content", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("user-token", { id: "owner" } as MeResponse);
  });

  afterEach(() => {
    useAuthStore.getState().logout();
    vi.unstubAllGlobals();
  });

  it("shares an overlapping authorized read but does not reuse a settled read or failure", async () => {
    const pending = deferredResponse();
    const failure = { reason: "mask_storage_unavailable", retryable: true };
    const fetch = vi
      .fn()
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(Response.json({ detail: failure }, { status: 503 }))
      .mockResolvedValueOnce(maskResponse(reference.sha256));
    vi.stubGlobal("fetch", fetch);

    const renderer = rasterMasksApi.annotationVideoMaskContent(annotation, 1, reference);
    const editor = rasterMasksApi.annotationVideoMaskContent({ ...annotation }, 1, {
      ...reference,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    pending.resolve(maskResponse(reference.sha256));
    await expect(Promise.all([renderer, editor])).resolves.toEqual([rle, rle]);

    await expect(
      rasterMasksApi.annotationVideoMaskContent(annotation, 1, reference),
    ).rejects.toMatchObject({
      status: 503,
      detailRaw: failure,
    });
    await expect(
      rasterMasksApi.annotationVideoMaskContent(annotation, 1, reference),
    ).resolves.toEqual(rle);
  });

  it("keeps a held frame and changed content independent and rejects a stale response", async () => {
    const original = deferredResponse();
    const held = deferredResponse();
    const next = deferredResponse();
    const replacement = { ...reference, object_key: "mask-2", sha256: "b".repeat(64) };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockReturnValueOnce(original.promise)
        .mockReturnValueOnce(held.promise)
        .mockReturnValueOnce(next.promise),
    );

    const staleRead = rasterMasksApi.annotationVideoMaskContent(annotation, 0, reference);
    const heldRead = rasterMasksApi.annotationVideoMaskContent(annotation, 1, reference);
    const nextRead = rasterMasksApi.annotationVideoMaskContent(
      { ...annotation, version: 2 },
      0,
      replacement,
    );
    const refused = expect(staleRead).rejects.toMatchObject({ status: 409 });

    next.resolve(maskResponse(replacement.sha256));
    held.resolve(maskResponse(reference.sha256));
    original.resolve(maskResponse(replacement.sha256));
    await Promise.all([
      refused,
      expect(nextRead).resolves.toEqual(rle),
      expect(heldRead).resolves.toEqual(rle),
    ]);
  });

  it("does not publish a late read after signing back in with the same credentials", async () => {
    const retired = deferredResponse();
    const current = deferredResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValueOnce(retired.promise).mockReturnValueOnce(current.promise),
    );

    const retiredRead = rasterMasksApi.annotationVideoMaskContent(annotation, 0, reference);
    const refused = expect(retiredRead).rejects.toMatchObject({ name: "AbortError" });
    useAuthStore.getState().logout();
    useAuthStore.getState().setAuth("user-token", { id: "owner" } as MeResponse);
    const currentRead = rasterMasksApi.annotationVideoMaskContent(annotation, 0, reference);

    current.resolve(maskResponse(reference.sha256));
    retired.resolve(maskResponse(reference.sha256));
    await Promise.all([refused, expect(currentRead).resolves.toEqual(rle)]);
  });
});
