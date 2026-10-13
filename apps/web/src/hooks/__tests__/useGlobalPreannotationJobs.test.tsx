/**
 * v0.9.13 · useGlobalPreannotationJobs WS smoke 测试.
 *
 * 兜底「14 个月没人发现 URL 写错」类 bug — 验证:
 *   - URL 路径 /ws/prediction-jobs?token=... 拼接正确
 *   - 非 admin 角色 / 无 token 不建连
 *   - 收消息后维护 jobs map
 *   - 卸载清理
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAuthStore } from "@/stores/authStore";

import { useGlobalPreannotationJobs } from "../useGlobalPreannotationJobs";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  // 与 WebSocket spec 对齐, useReconnectingWebSocket 卸载时按 readyState 判定 close
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  readyState = 0;
  closedManually = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close() {
    this.closedManually = true;
    this.onclose?.({ code: 1000, wasClean: true } as CloseEvent);
  }
  triggerOpen() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  triggerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

let originalWS: typeof globalThis.WebSocket | undefined;

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.useFakeTimers();
  originalWS = globalThis.WebSocket;
  Object.defineProperty(globalThis, "WebSocket", {
    value: MockWebSocket,
    writable: true,
    configurable: true,
  });
  useAuthStore.setState({
    token: "tok-123",
    user: { id: "u1", role: "super_admin" } as unknown as never,
  });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(globalThis, "WebSocket", {
    value: originalWS,
    writable: true,
    configurable: true,
  });
});

describe("useGlobalPreannotationJobs", () => {
  it("admin + token 时拼出 /ws/prediction-jobs URL", () => {
    renderHook(() => useGlobalPreannotationJobs());
    expect(MockWebSocket.instances.length).toBe(1);
    expect(MockWebSocket.instances[0].url).toMatch(/\/ws\/prediction-jobs/);
    expect(MockWebSocket.instances[0].url).toContain("token=tok-123");
  });

  it("无 token 不建连", () => {
    useAuthStore.setState({
      token: null,
      user: { id: "u1", role: "super_admin" } as unknown as never,
    });
    renderHook(() => useGlobalPreannotationJobs());
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("非 admin 角色不建连", () => {
    useAuthStore.setState({
      token: "tok-123",
      user: { id: "u1", role: "employee" } as unknown as never,
    });
    renderHook(() => useGlobalPreannotationJobs());
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("收到 running 消息后 runningJobs 反映", () => {
    const { result } = renderHook(() => useGlobalPreannotationJobs());
    act(() => {
      MockWebSocket.instances[0].triggerOpen();
      MockWebSocket.instances[0].triggerMessage({
        job_id: "j1",
        project_id: "p1",
        project_name: "P",
        status: "running",
        current: 5,
        total: 10,
      });
    });
    expect(result.current.runningJobs.length).toBe(1);
    expect(result.current.runningJobs[0].job_id).toBe("j1");
    expect(result.current.byProject["p1"]?.current).toBe(5);
  });

  it("ping 帧不触发 state 变化", () => {
    const { result } = renderHook(() => useGlobalPreannotationJobs());
    act(() => {
      MockWebSocket.instances[0].triggerMessage({ type: "ping" });
    });
    expect(result.current.runningJobs.length).toBe(0);
  });

  it("卸载时 close 主动断（已 OPEN 立即关）", () => {
    const { unmount } = renderHook(() => useGlobalPreannotationJobs());
    act(() => MockWebSocket.instances[0].triggerOpen());
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });

  it("卸载时仍在握手 → 延迟到 onopen 再关", () => {
    const { unmount } = renderHook(() => useGlobalPreannotationJobs());
    // 未 triggerOpen，readyState 仍 CONNECTING：直接关会触发浏览器告警，故延迟
    unmount();
    expect(MockWebSocket.instances[0].closedManually).toBe(false);
    act(() => MockWebSocket.instances[0].triggerOpen());
    expect(MockWebSocket.instances[0].closedManually).toBe(true);
  });
});
