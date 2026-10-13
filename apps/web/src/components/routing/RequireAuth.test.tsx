/**
 * Issue #123 · RequireAuth 兜底跳转与主动退出标记:
 * - 会话过期 / 未登录访问业务页 → /login 携带 state.from,登录后可返回原页;
 * - 主动退出(useLogout 已打标记)→ /login 不携带 from,避免把 /unauthorized
 *   或他人受限页留给下一个账号;
 * - 标记粘性置位(挂载期间每次重渲染都生效),恢复认证后清除,
 *   不影响之后的会话过期跳转。
 */
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { RequireAuth } from "./RequireAuth";
import { clearProactiveLogout, markProactiveLogout } from "@/utils/authRedirect";

const user: MeResponse = {
  id: "u1",
  email: "anno@example.com",
  name: "Anno",
  role: "employee",
  group_name: null,
  status: "active",
  created_at: "2026-05-10T00:00:00Z",
};

function LoginProbe() {
  const location = useLocation();
  return <div data-testid="login-probe" data-state={JSON.stringify(location.state ?? null)} />;
}

function renderProtected() {
  return render(
    <MemoryRouter initialEntries={["/datasets"]}>
      <Routes>
        <Route
          path="/datasets"
          element={
            <RequireAuth>
              <div>DATASETS</div>
            </RequireAuth>
          }
        />
        <Route path="/login" element={<LoginProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

function loginState(): unknown {
  return JSON.parse(screen.getByTestId("login-probe").getAttribute("data-state") ?? "null");
}

describe("RequireAuth 主动退出标记", () => {
  beforeEach(() => {
    // 粘性标记是模块级状态,测试间复位。
    clearProactiveLogout();
    useAuthStore.getState().setAuth("jwt", user);
  });

  it("主动退出(已打标记)→ /login 不携带 state.from", () => {
    renderProtected();
    expect(screen.getByText("DATASETS")).toBeInTheDocument();

    markProactiveLogout();
    act(() => {
      useAuthStore.setState({ token: null, user: null });
    });

    expect(screen.getByTestId("login-probe")).toBeInTheDocument();
    expect(loginState()).toBeNull();
  });

  it("会话过期(无标记)→ /login 携带 state.from 保留业务页", () => {
    renderProtected();

    act(() => {
      useAuthStore.setState({ token: null, user: null });
    });

    expect(screen.getByTestId("login-probe")).toBeInTheDocument();
    const state = loginState() as { from?: { pathname?: string } } | null;
    expect(state?.from?.pathname).toBe("/datasets");
  });

  it("标记粘性置位,恢复认证后清除,不影响之后的会话过期跳转", () => {
    const { unmount } = renderProtected();

    markProactiveLogout();
    act(() => {
      useAuthStore.setState({ token: null, user: null });
    });
    expect(loginState()).toBeNull();
    unmount();

    // 新一轮会话访问受保护页(RequireAuth 渲染到已登录分支清除标记),再过期:
    // from 应正常保留。
    useAuthStore.getState().setAuth("jwt2", user);
    renderProtected();
    expect(screen.getByText("DATASETS")).toBeInTheDocument();

    act(() => {
      useAuthStore.setState({ token: null, user: null });
    });
    const state = loginState() as { from?: { pathname?: string } } | null;
    expect(state?.from?.pathname).toBe("/datasets");
  });
});
