import { useEffect, useState } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { useLogin } from "@/hooks/useAuth";
import { useRegistrationStatus, useResendVerification } from "@/hooks/useInvitation";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Input } from "@/components/shadcn/ui/input";
import { Captcha } from "@/components/Captcha";
import { ApiError } from "@/api/client";
import { defaultHomePath, sanitizeLoginRedirect, clearProactiveLogout } from "@/utils/authRedirect";
import type { UserRole } from "@/types";

// v0.9.3 · 与后端 settings.login_captcha_threshold 同值；前端阈值仅做"何时渲染 Captcha"判断
const CAPTCHA_THRESHOLD = 5;

export function LoginPage() {
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const location = useLocation();
  const requestedNext = new URLSearchParams(location.search).get("next");
  // Issue #123 · 返回目标排除 /unauthorized 等错误页,无效目标回落角色默认首页,
  // 避免换账号登录后被带回上一个会话的拒绝提示页。MeResponse.role 为 string,
  // 与 usePermissions 一致按 UserRole 收窄。
  const from =
    sanitizeLoginRedirect(requestedNext) ??
    sanitizeLoginRedirect(
      (location.state as { from?: { pathname?: string } } | null)?.from?.pathname,
    ) ??
    defaultHomePath((user?.role ?? undefined) as UserRole | undefined);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [failedCount, setFailedCount] = useState(0);
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  // v0.12.0 · 后端返回 email_not_verified 时展示重发入口
  const [unverified, setUnverified] = useState(false);
  const [resendDone, setResendDone] = useState(false);
  const login = useLogin();
  const regStatus = useRegistrationStatus();
  const resend = useResendVerification();

  useEffect(() => {
    // Issue #123 · 登录页挂载即结束"主动退出"标记窗口:此后未登录直接打开业务
    // 链接时,RequireAuth 仍正常携带 from。挂载时机在退出跳转的 <Navigate>
    // 全部结束后,不会提前放行带 from 的覆盖跳转。
    clearProactiveLogout();
  }, []);

  useEffect(() => {
    if (login.isError) {
      const err = login.error;
      if (err instanceof ApiError) {
        const h = err.headers?.["x-login-failed-count"];
        const n = h ? parseInt(h, 10) : NaN;
        if (Number.isFinite(n)) setFailedCount(n);
        const code = (err.detailRaw as { code?: string } | undefined)?.code;
        setUnverified(code === "email_not_verified");
      }
    }
  }, [login.isError, login.error]);

  if (token) {
    const destination = user?.password_admin_reset_at ? "/settings" : from;
    return <Navigate to={destination} replace />;
  }

  const captchaRequired = failedCount >= CAPTCHA_THRESHOLD;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || !password) return;
    if (captchaRequired && !captchaToken) return;
    setUnverified(false);
    setResendDone(false);
    login.mutate(
      { email, password, captcha_token: captchaRequired ? captchaToken : undefined },
      {
        onSuccess: () => {
          setFailedCount(0);
          setCaptchaToken(null);
        },
      },
    );
  };

  const handleResend = () => {
    if (!email || resend.isPending || resendDone) return;
    resend.mutate(email, { onSuccess: () => setResendDone(true) });
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6 text-foreground">
      <div className="w-[min(380px,100%)]">
        {/* Logo */}
        <div className="mb-8 flex items-center justify-center gap-2.5">
          <img
            src="/ai-annotation-platform-icon.svg"
            alt=""
            aria-hidden="true"
            className="size-8 shrink-0 rounded-md"
          />
          <div>
            <div className="text-base font-bold">标注中心</div>
            <div className="text-xs text-muted-foreground">AI Annotation Platform</div>
          </div>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-border bg-card px-8 pb-8 pt-7 shadow-xl">
          <h1 className="mb-1 text-lg font-semibold">登录</h1>
          <p className="mb-6 text-sm text-muted-foreground">使用工作账号登录标注平台</p>

          {login.isError && (
            <div className="mb-4 flex items-center gap-2 rounded-md border border-rose-500/30 bg-status-danger-soft px-3 py-2.5 text-sm text-status-danger">
              <Icon name="warning" size={14} />
              {(login.error as Error)?.message ?? "登录失败，请检查账号密码"}
            </div>
          )}

          {unverified && (
            <div className="mb-4 flex items-center gap-2 rounded-md border border-rose-500/30 bg-status-danger-soft px-3 py-2.5 text-sm text-status-danger">
              {resendDone ? (
                "验证邮件已重新发送，请查收邮箱"
              ) : (
                <button
                  type="button"
                  onClick={handleResend}
                  disabled={resend.isPending}
                  className="appearance-none border-0 bg-transparent text-xs text-brand hover:underline"
                >
                  {resend.isPending ? "发送中…" : "重新发送验证邮件"}
                </button>
              )}
            </div>
          )}

          <form onSubmit={handleSubmit} className="flex flex-col gap-3.5">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-muted-foreground">账号</label>
              <Input
                type="text"
                autoComplete="username"
                placeholder="输入账号或邮箱"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium text-muted-foreground">密码</label>
              <div className="relative">
                <Input
                  type={showPwd ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  className="pr-9"
                />
                <button
                  type="button"
                  onClick={() => setShowPwd((v) => !v)}
                  className="absolute right-2.5 top-1/2 flex -translate-y-1/2 appearance-none items-center border-0 bg-transparent p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <Icon name={showPwd ? "eyeOff" : "eye"} size={14} />
                </button>
              </div>
            </div>

            {captchaRequired && (
              <div className="flex flex-col gap-1.5">
                <div className="text-xs text-muted-foreground">
                  连续失败已达 {failedCount} 次，请完成验证后重试
                </div>
                <Captcha onChange={setCaptchaToken} />
              </div>
            )}

            <Button
              type="submit"
              variant="primary"
              disabled={login.isPending || (captchaRequired && !captchaToken)}
              className="mt-1.5 w-full"
            >
              {login.isPending ? "登录中..." : "登录"}
            </Button>

            <div className="mt-2.5 flex items-center justify-between">
              {regStatus.data?.open_registration_enabled ? (
                <Link to="/register" className="text-xs text-brand hover:underline">
                  没有账号？立即注册
                </Link>
              ) : (
                <span />
              )}
              <Link to="/forgot-password" className="text-xs text-brand hover:underline">
                忘记密码？
              </Link>
            </div>
          </form>

          {import.meta.env.MODE !== "production" && (
            <div className="mt-5 rounded-md bg-muted px-3.5 py-3 text-xs text-muted-foreground">
              <div className="mb-1.5 font-medium text-muted-foreground">
                测试账号 (密码统一: 123456)
              </div>
              <div>
                超级管理员：<span className="mono">admin</span>
              </div>
              <div className="mt-0.5">
                项目管理员：<span className="mono">pm</span>
              </div>
              <div className="mt-0.5">
                质检员：<span className="mono">qa</span>
              </div>
              <div className="mt-0.5">
                标注员：<span className="mono">anno</span>
              </div>
              <div className="mt-0.5">
                观察者：<span className="mono">viewer</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
