import { StrictMode, useLayoutEffect } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { MeResponse } from "@/api/auth";
import { captureOriginalOwner, describeOriginal, type OriginalIdentity } from "@/api/originals";
import {
  prepareOriginalVideo,
  probeOriginalVideo,
  type OriginalVideoLease,
  type VideoOriginalResource,
} from "@/api/originalVideo";
import { useAuthStore } from "@/stores/authStore";
import { useOriginalVideoPlayback } from "./useOriginalVideoPlayback";

vi.mock("@/api/originals", async (actual) => ({
  ...(await actual<object>()),
  describeOriginal: vi.fn(),
}));
vi.mock("@/api/originalVideo", () => ({
  prepareOriginalVideo: vi.fn(),
  probeOriginalVideo: vi.fn(),
}));
const taskId = "11111111-1111-1111-1111-111111111111";
const nextTaskId = "22222222-2222-2222-2222-222222222222";
const contentId = "44444444-4444-4444-4444-444444444444";
const digest = "a".repeat(64);
const resource: VideoOriginalResource = { kind: "task", taskId };
const path = `/api/v1/files/tasks/${taskId}/original?content_id=${contentId}&sha256=${digest}`;
const identityPath = `/api/v1/files/tasks/${taskId}/original-identity`;
const identity: OriginalIdentity = {
  content_id: contentId,
  sha256: digest,
  size_bytes: 3,
  media_type: "video",
  owner_kind: "dataset_asset",
  original_availability: "unchecked",
  verification_state: "verified",
  download_path: path,
};
const events: string[] = [];
let serial = 0;
function lease(expiresAt = Date.now() + 300_000): OriginalVideoLease {
  const id = `55555555-5555-5555-5555-${String(++serial).padStart(12, "0")}`;
  return {
    leaseId: id,
    mediaPath: `/api/v1/media/original-playback/${id}`,
    expiresAt,
    identity,
    owner: captureOriginalOwner()!,
    release: vi.fn(async () => {
      events.push(`release:${id}`);
    }),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function nativeVideo() {
  const node = document.createElement("video");
  let playing = false;
  const pause = vi.fn(() => {
    playing = false;
    events.push("pause");
  });
  const load = vi.fn(() => {
    events.push(`load:${node.getAttribute("src")}`);
  });
  const play = vi.fn(async () => {
    playing = true;
    events.push("play");
  });
  Object.defineProperties(node, {
    paused: { get: () => !playing },
    ended: { get: () => false },
    readyState: { value: 4, configurable: true },
    error: { value: null, configurable: true },
    currentSrc: { get: () => (node.getAttribute("src") ? node.src : ""), configurable: true },
    pause: { value: pause },
    load: { value: load },
    play: { value: play },
  });
  return {
    node,
    pause,
    load,
    play,
    playing: (value: boolean) => {
      playing = value;
    },
  };
}
type Props = {
  resource: VideoOriginalResource | null;
  identityPath: string | null;
  video: HTMLVideoElement | null;
};
function mounted(video: HTMLVideoElement | null, extra: Partial<Props> = {}, strict = false) {
  return renderHook(
    (props: Props) => {
      const result = useOriginalVideoPlayback(props);
      useLayoutEffect(() => {
        if (props.video && result.mediaPath) props.video.setAttribute("src", result.mediaPath);
      }, [props.video, result.mediaPath]);
      return result;
    },
    {
      initialProps: { resource, identityPath, video, ...extra },
      ...(strict ? { wrapper: StrictMode } : {}),
    },
  );
}
const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 30; i++) await Promise.resolve();
  });
};
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-24T00:00:00Z"));
  vi.mocked(describeOriginal).mockReset().mockResolvedValue(identity);
  vi.mocked(prepareOriginalVideo)
    .mockReset()
    .mockImplementation(async () => lease());
  vi.mocked(probeOriginalVideo).mockReset().mockResolvedValue("expired");
  useAuthStore.getState().setAuth("private-token", { id: "alice" } as MeResponse);
  events.length = 0;
  serial = 0;
});
afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  vi.useRealTimers();
});

it.each([{ resource: null }, { identityPath: null }, { identityPath: "" }])(
  "does not prepare or fall back when disabled: %j",
  async (props) => {
    const { result } = mounted(null, props);
    await flush();
    expect(result.current).toMatchObject({
      status: "idle",
      mediaPath: undefined,
      contentKey: null,
    });
    expect(describeOriginal).not.toHaveBeenCalled();
    expect(prepareOriginalVideo).not.toHaveBeenCalled();
  },
);
it("canonicalizes fresh resource objects, keeps its lease through native node replacement and exposes exact source readiness", async () => {
  const first = nativeVideo();
  const { result, rerender } = mounted(first.node);
  await flush();
  const key = result.current.contentKey;
  expect(result.current.status).toBe("ready");
  expect(result.current.isSourceReady()).toBe(true);
  rerender({ resource: { kind: "task", taskId }, identityPath, video: null });
  expect(first.node.hasAttribute("src")).toBe(false);
  const second = nativeVideo();
  rerender({ resource: { kind: "task", taskId }, identityPath, video: second.node });
  await flush();
  expect(result.current.contentKey).toBe(key);
  expect(result.current.isSourceReady()).toBe(true);
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
  second.node.setAttribute("src", "/api/v1/media/original-playback/other");
  expect(result.current.isSourceReady()).toBe(false);
});
it("requires the exact expected identity path before preparing or exposing content", async () => {
  const { result } = mounted(null, { identityPath: identityPath + "&extra=value" });
  await flush();
  expect(result.current).toMatchObject({ status: "error", mediaPath: undefined, contentKey: null });
  expect(result.current.error).toContain("查询路径无效");
  expect(prepareOriginalVideo).not.toHaveBeenCalled();
});
it("StrictMode discards the first identity attempt and owns exactly one live prepared lease", async () => {
  const first = deferred<OriginalIdentity>();
  vi.mocked(describeOriginal).mockReturnValueOnce(first.promise).mockResolvedValueOnce(identity);
  const { result, unmount } = mounted(null, {}, true);
  await flush();
  first.resolve(identity);
  await flush();
  expect(result.current.status).toBe("ready");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
  const prepared = await vi.mocked(prepareOriginalVideo).mock.results[0].value;
  unmount();
  expect(prepared.release).toHaveBeenCalledTimes(1);
});
it.each(["task", "account", "session", "unmount"])(
  "retires a late successful lease after %s without repainting the next owner",
  async (change) => {
    const pending = deferred<OriginalVideoLease>();
    vi.mocked(prepareOriginalVideo).mockReturnValueOnce(pending.promise);
    const old = lease();
    const native = nativeVideo();
    const { result, rerender, unmount } = mounted(native.node);
    await flush();
    const signal = vi.mocked(prepareOriginalVideo).mock.calls[0][1]?.signal;
    if (change === "task") {
      vi.mocked(describeOriginal).mockResolvedValueOnce({
        ...identity,
        download_path: path.replace(taskId, nextTaskId),
      });
      rerender({
        resource: { kind: "task", taskId: nextTaskId },
        identityPath: identityPath.replace(taskId, nextTaskId),
        video: native.node,
      });
    } else if (change === "account")
      act(() => useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse));
    else if (change === "session")
      act(() => useAuthStore.setState({ sessionId: crypto.randomUUID() }));
    else unmount();
    await flush();
    const nextPath = result.current.mediaPath;
    pending.resolve(old);
    await flush();
    expect(signal?.aborted).toBe(true);
    expect(old.release).toHaveBeenCalledTimes(1);
    if (change !== "unmount") expect(result.current.mediaPath).toBe(nextPath);
  },
);
it("does not let an old late failure replace the next session's success", async () => {
  const pending = deferred<OriginalVideoLease>();
  vi.mocked(prepareOriginalVideo).mockReturnValueOnce(pending.promise);
  const { result } = mounted(null);
  await flush();
  act(() => useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse));
  await flush();
  const path = result.current.mediaPath;
  pending.reject(new ApiError(403, "old error"));
  await flush();
  expect(result.current).toMatchObject({ status: "ready", error: null, mediaPath: path });
});
it("detaches a playing native source before release on unmount and clears its timers", async () => {
  const native = nativeVideo();
  const current = lease();
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(current);
  const { unmount } = mounted(native.node);
  await flush();
  native.playing(true);
  events.length = 0;
  unmount();
  expect(events).toEqual(["pause", "load:null", `release:${current.leaseId}`]);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(600_000);
  });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
});
it("stops immediately on a raw storage token mismatch before adopting the complete next credentials", async () => {
  const native = nativeVideo();
  const current = lease();
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(current);
  const { result } = mounted(native.node);
  await flush();
  act(() => {
    localStorage.setItem("token", "other-tab-token");
    window.dispatchEvent(new StorageEvent("storage", { key: "token", storageArea: localStorage }));
    expect(native.node.hasAttribute("src")).toBe(false);
    expect(current.release).toHaveBeenCalledTimes(1);
  });
  expect(result.current.status).toBe("idle");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
  act(() => useAuthStore.getState().setToken("other-tab-token"));
  await flush();
  expect(result.current.status).toBe("ready");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(2);
});
it("keeps the logical source and lease on a legitimate same-session token refresh", async () => {
  const native = nativeVideo();
  const { result } = mounted(native.node);
  await flush();
  const key = result.current.contentKey;
  const source = result.current.mediaPath;
  act(() => useAuthStore.getState().setToken("renewed-token"));
  await flush();
  expect(result.current).toMatchObject({ contentKey: key, mediaPath: source, status: "ready" });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
});
it("keeps old playback while renewing; installs and releases in order, then resumes only after prime", async () => {
  const native = nativeVideo();
  const old = lease();
  const pending = deferred<OriginalVideoLease>();
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(old).mockReturnValueOnce(pending.promise);
  const { result } = mounted(native.node);
  await flush();
  const key = result.current.contentKey;
  native.playing(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(270_000);
  });
  expect(result.current).toMatchObject({ status: "refreshing", mediaPath: old.mediaPath });
  expect(native.pause).not.toHaveBeenCalled();
  expect(old.release).not.toHaveBeenCalled();
  const next = lease();
  events.length = 0;
  pending.resolve(next);
  await flush();
  expect(events).toEqual(["pause", "load:null", `release:${old.leaseId}`]);
  expect(result.current).toMatchObject({
    contentKey: key,
    mediaPath: next.mediaPath,
    status: "ready",
  });
  expect(native.play).not.toHaveBeenCalled();
  act(() => result.current.onPrimed());
  expect(native.play).toHaveBeenCalledTimes(1);
  act(() => result.current.onPrimed());
  expect(native.play).toHaveBeenCalledTimes(1);
});
it("does not resurrect playback when explicit pause cancels resume during renewal", async () => {
  const native = nativeVideo();
  const pending = deferred<OriginalVideoLease>();
  vi.mocked(prepareOriginalVideo)
    .mockResolvedValueOnce(lease())
    .mockReturnValueOnce(pending.promise);
  const { result } = mounted(native.node);
  await flush();
  native.playing(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(270_000);
  });
  act(() => result.current.cancelResume());
  pending.resolve(lease());
  await flush();
  act(() => result.current.onPrimed());
  expect(native.play).not.toHaveBeenCalled();
});
it("detaches at actual expiry even when renewal is still pending, preserving only intended resume", async () => {
  const native = nativeVideo();
  const old = lease();
  const pending = deferred<OriginalVideoLease>();
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(old).mockReturnValueOnce(pending.promise);
  const { result } = mounted(native.node);
  await flush();
  native.playing(true);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300_000);
  });
  expect(result.current).toMatchObject({ status: "preparing", mediaPath: undefined });
  expect(native.node.hasAttribute("src")).toBe(false);
  expect(old.release).toHaveBeenCalledTimes(1);
  pending.resolve(lease());
  await flush();
  act(() => result.current.onPrimed());
  expect(native.play).toHaveBeenCalledTimes(1);
});
it("waits until a short authority cap instead of busy-renewing on timers or repeated focus", async () => {
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(lease(Date.now() + 10_000));
  mounted(null);
  await flush();
  act(() => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("focus"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(9999);
  });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(2);
});
it("does not repeatedly renew a new lease whose expiry improves by less than one second", async () => {
  const oldExpiry = Date.now() + 300_000;
  vi.mocked(prepareOriginalVideo)
    .mockResolvedValueOnce(lease(oldExpiry))
    .mockResolvedValueOnce(lease(oldExpiry + 500));
  mounted(null);
  await flush();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(270_000);
  });
  act(() => {
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_499);
  });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(3);
});
it("uses a foreground event to recover a wall-clock-expired lease when background timers were throttled", async () => {
  const native = nativeVideo();
  const old = lease();
  vi.mocked(prepareOriginalVideo).mockResolvedValueOnce(old);
  const { result } = mounted(native.node);
  await flush();
  act(() => {
    vi.setSystemTime(Date.now() + 310_000);
    window.dispatchEvent(new Event("focus"));
  });
  await flush();
  expect(old.release).toHaveBeenCalledTimes(1);
  expect(result.current.status).toBe("ready");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(2);
});
it("bounds native 410 recovery until a successful current-source prime resets the budget", async () => {
  const native = nativeVideo();
  const { result } = mounted(native.node);
  await flush();
  for (let i = 0; i < 2; i++) {
    act(() => {
      result.current.recover();
      result.current.recover();
    });
    await flush();
  }
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(3);
  act(() => result.current.recover());
  await flush();
  expect(result.current.status).toBe("error");
  expect(result.current.error).toContain("连续失效");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(3);
  const key = result.current.contentKey;
  act(() => result.current.retry());
  await flush();
  expect(result.current).toMatchObject({ status: "ready", contentKey: key });
  act(() => result.current.onPrimed());
  act(() => result.current.recover());
  await flush();
  expect(result.current.status).toBe("ready");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(5);
});
it.each(["valid", "forbidden"])(
  "surfaces %s probes without silently preparing another original",
  async (scenario) => {
    if (scenario === "valid") vi.mocked(probeOriginalVideo).mockResolvedValueOnce("valid");
    else vi.mocked(probeOriginalVideo).mockRejectedValueOnce(new ApiError(403, "权限已撤销"));
    const { result } = mounted(null);
    await flush();
    const key = result.current.contentKey;
    act(() => result.current.recover());
    await flush();
    expect(result.current).toMatchObject({
      status: "error",
      mediaPath: undefined,
      contentKey: key,
    });
    expect(result.current.error).toContain(scenario === "valid" ? "无法解码" : "权限已撤销");
    expect(prepareOriginalVideo).toHaveBeenCalledTimes(1);
  },
);
it("retains content identity and allows explicit retry after a renewal failure", async () => {
  vi.mocked(prepareOriginalVideo)
    .mockResolvedValueOnce(lease())
    .mockRejectedValueOnce(new ApiError(429, "播放存储已满"));
  const { result } = mounted(null);
  await flush();
  const key = result.current.contentKey;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(270_000);
  });
  expect(result.current).toMatchObject({
    status: "error",
    contentKey: key,
    identity,
    error: "播放存储已满",
  });
  act(() => result.current.retry());
  await flush();
  expect(result.current).toMatchObject({ status: "ready", contentKey: key });
  expect(describeOriginal).toHaveBeenCalledTimes(2);
});
it("reprepares under refreshed credentials when the old authority proof fails, including refresh during HEAD", async () => {
  const response = deferred<"valid" | "expired">();
  vi.mocked(probeOriginalVideo).mockReturnValueOnce(response.promise);
  const { result } = mounted(null);
  await flush();
  act(() => result.current.recover());
  await flush();
  act(() => useAuthStore.getState().setToken("new-token"));
  response.reject(new DOMException("token changed", "AbortError"));
  await flush();
  expect(result.current.status).toBe("ready");
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(2);
  act(() => useAuthStore.getState().setToken("newer-token"));
  act(() => result.current.recover());
  await flush();
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(3);
  expect(probeOriginalVideo).toHaveBeenCalledTimes(1);
});
it("ignores late HEAD failures after a task switch and cannot detach a newly bound source", async () => {
  const response = deferred<"valid" | "expired">();
  vi.mocked(probeOriginalVideo).mockReturnValueOnce(response.promise);
  const native = nativeVideo();
  const { result, rerender } = mounted(native.node);
  await flush();
  act(() => result.current.recover());
  await flush();
  vi.mocked(describeOriginal).mockResolvedValueOnce({
    ...identity,
    download_path: path.replace(taskId, nextTaskId),
  });
  rerender({
    resource: { kind: "task", taskId: nextTaskId },
    identityPath: identityPath.replace(taskId, nextTaskId),
    video: native.node,
  });
  await flush();
  const newPath = result.current.mediaPath;
  response.reject(new ApiError(403, "old failure"));
  await flush();
  expect(result.current).toMatchObject({ status: "ready", mediaPath: newPath, error: null });
  expect(native.node.getAttribute("src")).toBe(newPath);
});

it("updates only the logical content key when explicit retry discovers another verified content identity", async () => {
  vi.mocked(probeOriginalVideo).mockRejectedValueOnce(new ApiError(409, "内容已变更"));
  const { result } = mounted(null);
  await flush();
  const before = result.current.contentKey;
  act(() => result.current.recover());
  await flush();
  const nextIdentity = {
    ...identity,
    content_id: taskId,
    sha256: "b".repeat(64),
    download_path: path.replace(contentId, taskId).replace(digest, "b".repeat(64)),
  };
  vi.mocked(describeOriginal).mockResolvedValueOnce(nextIdentity);
  act(() => result.current.retry());
  await flush();
  expect(result.current.status).toBe("ready");
  expect(result.current.identity).toEqual(nextIdentity);
  expect(result.current.contentKey).not.toBe(before);
  expect(result.current.contentKey).not.toContain("private-token");
});
it("resets the automatic recovery budget only after the current native source is primed", async () => {
  const native = nativeVideo();
  const { result } = mounted(native.node);
  await flush();
  for (let i = 0; i < 4; i++) {
    act(() => result.current.onPrimed());
    act(() => result.current.recover());
    await flush();
    expect(result.current.status).toBe("ready");
  }
  expect(prepareOriginalVideo).toHaveBeenCalledTimes(5);
});
it("does not detach a node that a later owner has already rebound before cleanup", async () => {
  const native = nativeVideo();
  const { unmount } = mounted(native.node);
  await flush();
  const replacement = "/api/v1/media/original-playback/66666666-6666-6666-6666-666666666666";
  native.node.setAttribute("src", replacement);
  events.length = 0;
  unmount();
  expect(native.node.getAttribute("src")).toBe(replacement);
  expect(events).toHaveLength(1);
  expect(events[0]).toMatch(/^release:/);
});
