import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams, Navigate } from "react-router-dom";
import { invitationsApi } from "@/api/invitations";
import { Icon } from "@/components/ui/Icon";

type Phase = "verifying" | "done" | "error";

export function VerifyEmailPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token");

  const [phase, setPhase] = useState<Phase>("verifying");
  const [error, setError] = useState("");
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    invitationsApi
      .verifyEmail(token)
      .then(() => setPhase("done"))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : "验证失败");
        setPhase("error");
      });
  }, [token]);

  if (!token) return <Navigate to="/login" replace />;

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6 text-foreground">
      <div className="w-[min(380px,100%)]">
        <div className="mb-8 flex items-center justify-center gap-2.5">
          <div className="flex size-8 items-center justify-center rounded-md bg-brand text-white">
            <Icon name="lock" size={16} />
          </div>
          <span className="text-lg font-bold">邮箱验证</span>
        </div>

        <div className="rounded-2xl border border-border bg-card px-8 py-7 text-center shadow-xl">
          {phase === "verifying" && (
            <div className="text-sm text-muted-foreground">正在验证邮箱…</div>
          )}

          {phase === "done" && (
            <>
              <div className="mb-4 text-sm text-muted-foreground">邮箱已验证，现在可以登录了。</div>
              <Link to="/login" className="text-sm text-brand hover:underline">
                前往登录
              </Link>
            </>
          )}

          {phase === "error" && (
            <>
              <div className="mb-4 text-sm text-status-danger">
                {error || "验证链接无效或已过期"}
              </div>
              <Link to="/login" className="text-sm text-brand hover:underline">
                返回登录
              </Link>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
