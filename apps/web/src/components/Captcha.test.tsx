/**
 * Captcha 组件单测：site key 缺省时不渲染；token 回调透传；卸载清理 widget。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render } from "@testing-library/react";

const mockGetSiteKey = vi.fn<() => string | null>();
const mockRender = vi.fn(async () => "widget-id-1");
const mockRemove = vi.fn();

vi.mock("@/lib/turnstile", () => ({
  getTurnstileSiteKey: () => mockGetSiteKey(),
  renderTurnstile: () => mockRender(),
  removeTurnstile: (id: string | null) => mockRemove(id),
}));

import { Captcha, isCaptchaRequired } from "./Captcha";

describe("Captcha", () => {
  beforeEach(() => {
    mockGetSiteKey.mockReset();
    mockRender.mockClear();
    mockRemove.mockClear();
  });
  afterEach(() => {
    mockGetSiteKey.mockReset();
  });

  it("site key 缺省时不渲染 widget 容器", () => {
    mockGetSiteKey.mockReturnValue(null);
    const onChange = vi.fn();
    const { queryByTestId } = render(<Captcha onChange={onChange} />);
    expect(queryByTestId("captcha-widget")).toBeNull();
    expect(mockRender).not.toHaveBeenCalled();
    expect(isCaptchaRequired()).toBe(false);
  });

  it("site key 配置时渲染容器并调用 renderTurnstile", () => {
    mockGetSiteKey.mockReturnValue("0x4AAA...sitekey");
    const onChange = vi.fn();
    const { getByTestId } = render(<Captcha onChange={onChange} />);
    expect(getByTestId("captcha-widget")).toBeTruthy();
    expect(mockRender).toHaveBeenCalledTimes(1);
    expect(isCaptchaRequired()).toBe(true);
  });

  it("卸载时调用 removeTurnstile 清理 widget", async () => {
    mockGetSiteKey.mockReturnValue("0x4AAA...sitekey");
    const onChange = vi.fn();
    const { unmount } = render(<Captcha onChange={onChange} />);
    // 等 microtask 让 renderTurnstile resolve 写入 widgetId
    await Promise.resolve();
    await Promise.resolve();
    unmount();
    expect(mockRemove).toHaveBeenCalled();
  });
});
