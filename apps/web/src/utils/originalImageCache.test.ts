import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as originals from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";
import { readOriginalImage } from "./originalImageCache";

vi.mock("@/api/originals", async (load) => {
  const actual = await load<typeof import("@/api/originals")>();
  return {
    ...actual,
    describeOriginal: vi.fn(),
    readOriginal: vi.fn(),
    promoteOriginalRead: vi.fn(),
  };
});
const user = { id: "image-cache-reader" } as Parameters<
  ReturnType<typeof useAuthStore.getState>["setAuth"]
>[1];
const target = { kind: "task" as const, taskId: "one" };
const blob = new Blob(["GIF89a"]);
let generation = 1;
function identity(resource: originals.OriginalResource): originals.OriginalIdentity {
  return {
    owner_kind: "dataset_asset",
    content_id: "content",
    sha256: "a".repeat(64),
    size_bytes: blob.size,
    media_type: "image",
    verification_state: "verified",
    original_availability: "unchecked",
    download_path: `${JSON.stringify(resource)}/${generation}`,
  };
}
function options(priority: "active" | "prefetch" = "active", controller = new AbortController()) {
  return { owner: originals.captureOriginalOwner()!, signal: controller.signal, priority };
}
beforeEach(() => {
  useAuthStore.getState().setAuth("one", user);
  generation = 1;
  vi.mocked(originals.describeOriginal)
    .mockReset()
    .mockImplementation(async (resource) => identity(resource));
  vi.mocked(originals.readOriginal)
    .mockReset()
    .mockImplementation(async (resource) => ({ identity: identity(resource), blob }));
  vi.mocked(originals.promoteOriginalRead).mockClear();
});
afterEach(() => useAuthStore.getState().logout());
describe("bounded owned image prefetch", () => {
  it("re-describes active reads and reuses only the same resource/content bytes", async () => {
    await readOriginalImage(target, options("prefetch"));
    await readOriginalImage(target, options());
    expect(originals.describeOriginal).toHaveBeenCalledTimes(2);
    expect(originals.readOriginal).toHaveBeenCalledTimes(1);
    generation++;
    await readOriginalImage(target, options());
    expect(originals.readOriginal).toHaveBeenCalledTimes(2);
  });
  it("joins private pending work without one consumer cancelling another", async () => {
    let complete!: (value: { identity: originals.OriginalIdentity; blob: Blob }) => void;
    vi.mocked(originals.readOriginal).mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const first = new AbortController();
    const pending = readOriginalImage(target, options("prefetch", first));
    const active = readOriginalImage(target, options());
    await vi.waitFor(() => expect(originals.readOriginal).toHaveBeenCalledTimes(1));
    first.abort();
    await expect(pending).rejects.toHaveProperty("name", "AbortError");
    const privateSignal = vi.mocked(originals.readOriginal).mock.calls[0][1]?.signal;
    expect(privateSignal?.aborted).toBe(false);
    complete({ identity: identity(target), blob });
    expect((await active)?.blob).toBe(blob);
    expect(originals.promoteOriginalRead).toHaveBeenCalledWith(privateSignal);
  });
  it("cancels private work when its last consumer leaves", async () => {
    vi.mocked(originals.readOriginal).mockImplementation(
      (_, { signal } = {}) =>
        new Promise((_, reject) =>
          signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          ),
        ),
    );
    const controller = new AbortController();
    const pending = readOriginalImage(target, options("prefetch", controller));
    await vi.waitFor(() => expect(originals.readOriginal).toHaveBeenCalledTimes(1));
    controller.abort();
    await expect(pending).rejects.toHaveProperty("name", "AbortError");
    expect(vi.mocked(originals.readOriginal).mock.calls[0][1]?.signal?.aborted).toBe(true);
  });
  it("keeps at most two ready speculative entries", async () => {
    for (const taskId of ["one", "two", "three"])
      await readOriginalImage({ kind: "task", taskId }, options("prefetch"));
    await readOriginalImage(target, options());
    expect(originals.readOriginal).toHaveBeenCalledTimes(4);
  });
  it("skips oversized speculative originals before starting a download", async () => {
    vi.mocked(originals.describeOriginal).mockResolvedValue({
      ...identity(target),
      size_bytes: 64 * 1024 * 1024 + 1,
    });
    expect(await readOriginalImage(target, options("prefetch"))).toBeNull();
    expect(originals.readOriginal).not.toHaveBeenCalled();
  });
  it("evicts older ready bytes when two entries exceed the raw prefetch budget", async () => {
    const large = new Blob([new Uint8Array(33 * 1024 * 1024)]);
    vi.mocked(originals.describeOriginal).mockImplementation(async (resource) => ({
      ...identity(resource),
      size_bytes: large.size,
    }));
    vi.mocked(originals.readOriginal).mockImplementation(async (resource) => ({
      identity: { ...identity(resource), size_bytes: large.size },
      blob: large,
    }));
    await readOriginalImage(target, options("prefetch"));
    await readOriginalImage({ kind: "task", taskId: "two" }, options("prefetch"));
    await readOriginalImage(target, options());
    expect(originals.readOriginal).toHaveBeenCalledTimes(3);
  });
  it("retains bytes on token refresh and clears them for the next login", async () => {
    await readOriginalImage(target, options());
    useAuthStore.getState().setToken("refreshed");
    await readOriginalImage(target, options());
    expect(originals.readOriginal).toHaveBeenCalledTimes(1);
    useAuthStore.getState().setAuth("next-login", user);
    await readOriginalImage(target, options());
    expect(originals.readOriginal).toHaveBeenCalledTimes(2);
  });
});
