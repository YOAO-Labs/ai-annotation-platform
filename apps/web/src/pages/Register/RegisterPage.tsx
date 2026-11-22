import { useState } from "react";
import { flushSync } from "react-dom";
import { clsx } from "clsx";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Captcha, isCaptchaRequired } from "@/components/Captcha";
import {
  useResolveInvitation,
  useRegister,
  useAcceptExistingInvitation,
  useRegistrationStatus,
  useOpenRegister,
  useResendVerification,
} from "@/hooks/useInvitation";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { ROLE_LABELS } from "@/constants/roles";
import type { UserRole } from "@/types";
import type { ApiError } from "@/api/client";
import type { InvitationAcceptance, RegisterResponse } from "@/api/invitations";
import { getPasswordRequirements, isPasswordStrong } from "@/utils/password";
import styles from "./RegisterPage.module.css";

function PasswordStrengthIndicator({ pwd }: { pwd: string }) {
  if (!pwd) return null;
  const rules = getPasswordRequirements(pwd);
  return (
    <div className={styles.passwordRules}>
      {rules.map((r) => (
        <span key={r.label} className={r.ok ? styles.passwordRuleOk : styles.passwordRuleBad}>
          {r.ok ? "✓" : "✗"} {r.label}
        </span>
      ))}
    </div>
  );
}

export function RegisterPage() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const existingToken = useAuthStore((s) => s.token);
  const currentUserId = useAuthStore((s) => s.user?.id);
  const [hadAuthOnEntry] = useState(() => Boolean(useAuthStore.getState().token));

  if (hadAuthOnEntry && existingToken) {
    return token ? (
      <ExistingAccountInviteForm key={`${token}:${currentUserId}`} token={token} />
    ) : (
      <Navigate to="/dashboard" replace />
    );
  }

  if (token) {
    return <InviteRegisterForm key={token} token={token} />;
  }
  return <OpenRegisterForm />;
}

function OpenRegisterForm() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((s) => s.setAuth);
  const regStatus = useRegistrationStatus();
  const openRegister = useOpenRegister();

  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [pwd, setPwd] = useState("");
  const [pwd2, setPwd2] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [verificationSentTo, setVerificationSentTo] = useState<string | null>(null);
  const captchaRequired = isCaptchaRequired();

  if (regStatus.isLoading) {
    return (
      <CenteredCard>
        <span className={styles.mutedText}>加载中…</span>
      </CenteredCard>
    );
  }

  if (!regStatus.data?.open_registration_enabled) {
    return <ErrorPanel title="注册未开放" hint="当前不支持自助注册，请联系管理员获取邀请链接。" />;
  }

  if (verificationSentTo) {
    return <VerificationSentPanel email={verificationSentTo} />;
  }

  const passwordsMatch = !pwd || !pwd2 || pwd === pwd2;
  const passwordsValid = isPasswordStrong(pwd);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !name.trim() || !passwordsValid || pwd !== pwd2) return;
    if (captchaRequired && !captchaToken) return;
    openRegister.mutate(
      {
        email: email.trim(),
        name: name.trim(),
        password: pwd,
        captcha_token: captchaToken,
      },
      {
        onSuccess: (data) => {
          if (data.email_verification_required || !data.access_token) {
            setVerificationSentTo(email.trim());
            return;
          }
          setAuth(data.access_token, data.user);
          navigate("/dashboard", { replace: true });
        },
      },
    );
  };

  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <h1 className={styles.title}>注册账号</h1>
        <p className={styles.description}>创建账号后默认为观察者角色，管理员可为你分配更高权限。</p>

        {openRegister.isError && <ErrorBanner msg={(openRegister.error as Error).message} />}

        <form onSubmit={submit} className={styles.form}>
          <Field label="邮箱">
            <input
              required
              autoFocus
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              maxLength={255}
              placeholder="your@email.com"
              className={styles.input}
            />
          </Field>

          <Field label="姓名">
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              placeholder="如何在平台中称呼你"
              className={styles.input}
            />
          </Field>

          <Field label="密码（至少 8 位，需含大小写字母和数字）">
            <div className={styles.passwordField}>
              <input
                required
                type={showPwd ? "text" : "password"}
                value={pwd}
                onChange={(e) => setPwd(e.target.value)}
                minLength={8}
                maxLength={128}
                className={clsx(styles.input, styles.passwordInput)}
              />
              <button
                type="button"
                onClick={() => setShowPwd((v) => !v)}
                className={styles.eyeButton}
                aria-label="切换密码可见性"
              >
                <Icon name={showPwd ? "eyeOff" : "eye"} size={14} />
              </button>
            </div>
            <PasswordStrengthIndicator pwd={pwd} />
          </Field>

          <Field label="再次输入密码">
            <input
              required
              type={showPwd ? "text" : "password"}
              value={pwd2}
              onChange={(e) => setPwd2(e.target.value)}
              maxLength={128}
              className={clsx(styles.input, !passwordsMatch && styles.inputInvalid)}
            />
            {!passwordsMatch && <div className={styles.mismatchText}>两次密码不一致</div>}
          </Field>

          <Captcha onChange={setCaptchaToken} />

          <button
            type="submit"
            disabled={
              !email.trim() ||
              !name.trim() ||
              !passwordsValid ||
              !passwordsMatch ||
              openRegister.isPending ||
              (captchaRequired && !captchaToken)
            }
            className={clsx(
              styles.primaryButton,
              openRegister.isPending && styles.primaryButtonPending,
            )}
          >
            {openRegister.isPending ? "注册中..." : "注册"}
          </button>
        </form>

        <div className={styles.loginPrompt}>
          <span className={styles.mutedText}>已有账号？</span>{" "}
          <a href="/login" className={styles.link}>
            立即登录
          </a>
        </div>
      </div>
    </CenteredCard>
  );
}

function InviteRegisterForm({ token }: { token: string }) {
  const navigate = useNavigate();
  const setAuth = useAuthStore((s) => s.setAuth);
  const [accepted, setAccepted] = useState<RegisterResponse | null>(null);
  const resolve = useResolveInvitation(accepted ? null : token);
  const register = useRegister();

  const [name, setName] = useState("");
  const [pwd, setPwd] = useState("");
  const [pwd2, setPwd2] = useState("");
  const [showPwd, setShowPwd] = useState(false);

  if (accepted) return <InviteAcceptedPanel acceptance={accepted.acceptance} />;

  if (resolve.isLoading) {
    return (
      <CenteredCard>
        <span className={styles.mutedText}>正在校验邀请链接…</span>
      </CenteredCard>
    );
  }

  if (resolve.isError) {
    const err = resolve.error as ApiError;
    const status = err?.status;
    const detail =
      status === 404
        ? "邀请链接无效"
        : status === 410
          ? (err.message ?? "该邀请已失效")
          : (err?.message ?? "无法读取邀请信息");
    return <ErrorPanel title={detail} hint="请联系管理员重新发送邀请。" />;
  }

  const inv = resolve.data!;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !isPasswordStrong(pwd) || pwd !== pwd2) return;
    const ownerToken = useAuthStore.getState().token;
    register.mutate(
      { token, name: name.trim(), password: pwd },
      {
        onSuccess: (data) => {
          if (
            !data.access_token ||
            useAuthStore.getState().token !== ownerToken ||
            localStorage.getItem("token") !== ownerToken
          )
            return;
          if (data.acceptance) flushSync(() => setAccepted(data));
          setAuth(data.access_token, data.user);
          if (!data.acceptance) navigate("/dashboard", { replace: true });
        },
      },
    );
  };

  const passwordsMatch = !pwd || !pwd2 || pwd === pwd2;
  const passwordsValid = isPasswordStrong(pwd);

  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <h1 className={styles.title}>设置你的账号</h1>
        <p className={styles.description}>
          来自 <strong>{inv.invited_by_name ?? "管理员"}</strong> 的邀请，绑定邮箱{" "}
          <span className={clsx("mono", styles.inviteEmail)}>{inv.email}</span>
        </p>

        <div className={styles.pillRow}>
          <Pill>{ROLE_LABELS[inv.role as UserRole] ?? inv.role}</Pill>
          {inv.group_name && <Pill>{inv.group_name}</Pill>}
          {inv.project_name && <Pill>项目：{inv.project_name}</Pill>}
          <Pill>有效期至 {new Date(inv.expires_at).toLocaleString("zh-CN")}</Pill>
        </div>

        {register.isError && <ErrorBanner msg={(register.error as Error).message} />}

        <form onSubmit={submit} className={styles.form}>
          <Field label="姓名">
            <input
              required
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              placeholder="如何在平台中称呼你"
              className={styles.input}
            />
          </Field>

          <Field label="密码（至少 8 位，需含大小写字母和数字）">
            <div className={styles.passwordField}>
              <input
                required
                type={showPwd ? "text" : "password"}
                value={pwd}
                onChange={(e) => setPwd(e.target.value)}
                minLength={8}
                maxLength={128}
                className={clsx(styles.input, styles.passwordInput)}
              />
              <button
                type="button"
                onClick={() => setShowPwd((v) => !v)}
                className={styles.eyeButton}
                aria-label="切换密码可见性"
              >
                <Icon name={showPwd ? "eyeOff" : "eye"} size={14} />
              </button>
            </div>
            <PasswordStrengthIndicator pwd={pwd} />
          </Field>

          <Field label="再次输入密码">
            <input
              required
              type={showPwd ? "text" : "password"}
              value={pwd2}
              onChange={(e) => setPwd2(e.target.value)}
              maxLength={128}
              className={clsx(styles.input, !passwordsMatch && styles.inputInvalid)}
            />
            {!passwordsMatch && <div className={styles.mismatchText}>两次密码不一致</div>}
          </Field>

          <button
            type="submit"
            disabled={!name.trim() || !passwordsValid || !passwordsMatch || register.isPending}
            className={clsx(
              styles.primaryButton,
              register.isPending && styles.primaryButtonPending,
            )}
          >
            {register.isPending ? "创建中..." : "完成注册并登录"}
          </button>
        </form>

        <div className={styles.loginPrompt}>
          已有账号？
          <a
            href={`/login?next=${encodeURIComponent(`/register?token=${token}`)}`}
            className={styles.link}
          >
            登录后确认加入
          </a>
        </div>
      </div>
    </CenteredCard>
  );
}

function ExistingAccountInviteForm({ token }: { token: string }) {
  const navigate = useNavigate();
  const currentUser = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const resolve = useResolveInvitation(token);
  const accept = useAcceptExistingInvitation();
  const [accepted, setAccepted] = useState<InvitationAcceptance | null>(null);

  if (accepted) return <InviteAcceptedPanel acceptance={accepted} />;
  if (resolve.isLoading) {
    return (
      <CenteredCard>
        <span className={styles.mutedText}>正在校验邀请链接…</span>
      </CenteredCard>
    );
  }
  if (resolve.isError) {
    return (
      <ErrorPanel
        title={invitationError(resolve.error as ApiError)}
        hint="请联系管理员重新发送邀请。"
      />
    );
  }

  const inv = resolve.data!;
  if (!inv.project_id) {
    return (
      <ErrorPanel
        title="该邀请用于创建新账号"
        hint="当前账号已经存在。请联系管理员创建带目标项目的邀请，再确认加入项目。"
      />
    );
  }
  const emailMatches = currentUser?.email.trim().toLowerCase() === inv.email.trim().toLowerCase();
  const submit = () => {
    const ownerId = currentUser?.id;
    if (!emailMatches || !ownerId || !isCurrentAuthOwner(ownerId)) return;
    accept.mutate(token, {
      onSuccess: (data) => {
        if (!isCurrentAuthOwner(ownerId)) return;
        setUser(data.user);
        setAccepted(data.acceptance);
      },
    });
  };

  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <h1 className={styles.title}>确认加入项目</h1>
        <p className={styles.description}>
          当前登录账号{" "}
          <span className={clsx("mono", styles.inviteEmail)}>{currentUser?.email}</span>{" "}
          {emailMatches
            ? "与邀请邮箱一致。确认后会保留你的全局角色，只新增目标项目成员关系。"
            : `与邀请邮箱 ${inv.email} 不一致。请返回首页退出当前账号，再使用被邀请邮箱登录。`}
        </p>
        <div className={styles.pillRow}>
          <Pill>{ROLE_LABELS[inv.role as UserRole] ?? inv.role}</Pill>
          {inv.group_name && <Pill>{inv.group_name}</Pill>}
          {inv.project_name && <Pill>项目：{inv.project_name}</Pill>}
          <Pill>有效期至 {new Date(inv.expires_at).toLocaleString("zh-CN")}</Pill>
        </div>
        {accept.isError && <ErrorBanner msg={(accept.error as Error).message} />}
        <button
          type="button"
          onClick={submit}
          disabled={accept.isPending || !emailMatches}
          className={clsx(styles.primaryButton, accept.isPending && styles.primaryButtonPending)}
        >
          {accept.isPending ? "确认中…" : "确认并加入"}
        </button>
        <div className={styles.loginPrompt}>
          <button type="button" onClick={() => navigate("/dashboard")} className={styles.link}>
            返回首页
          </button>
        </div>
      </div>
    </CenteredCard>
  );
}

function InviteAcceptedPanel({ acceptance }: { acceptance?: InvitationAcceptance | null }) {
  const navigate = useNavigate();
  const projectId = acceptance?.project_id;
  const memberRole = acceptance?.project_member_role;
  const workPath = projectId
    ? memberRole === "reviewer"
      ? `/projects/${projectId}/review`
      : memberRole === "annotator"
        ? `/projects/${projectId}/annotate`
        : `/projects/${projectId}/data-manager`
    : "/dashboard";
  const hasWork = acceptance?.next_action === "start_work";

  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <h1 className={styles.title}>邀请已完成</h1>
        <p className={styles.description}>
          {acceptance?.project_name
            ? `你已加入项目「${acceptance.project_name}」。`
            : "账号已创建，管理员可以继续为你分配项目。"}
        </p>
        {acceptance?.project_name && (
          <div className={styles.acceptanceStatus}>
            <div className={styles.acceptanceStatusTitle}>
              {hasWork ? "已有可开始的工作" : acceptance.next_action_label}
            </div>
            <div className={styles.acceptanceStatusText}>
              {hasWork
                ? `当前有 ${acceptance.active_batch_count} 个活动批次。`
                : `项目负责人：${acceptance.responsible_person_name ?? "待指定"}。加入项目本身不会自动产生激活批次。`}
            </div>
          </div>
        )}
        <button type="button" onClick={() => navigate(workPath)} className={styles.primaryButton}>
          {hasWork ? "开始工作" : acceptance?.project_id ? "打开项目" : "进入首页"}
        </button>
      </div>
    </CenteredCard>
  );
}

function invitationError(error: ApiError): string {
  if (error.status === 404) return "邀请链接无效";
  if (error.status === 410) return error.message || "该邀请已失效";
  return error.message || "无法读取邀请信息";
}

function Brand() {
  return (
    <div className={styles.brand}>
      <img
        src="/ai-annotation-platform-icon.svg"
        alt=""
        aria-hidden="true"
        className={styles.brandIcon}
      />
      <div>
        <div className={styles.brandTitle}>YOAO</div>
        <div className={styles.brandSubtitle}>You Only Annotate Once</div>
      </div>
    </div>
  );
}

function CenteredCard({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.page}>
      <div className={styles.shell}>{children}</div>
    </div>
  );
}

function ErrorPanel({ title, hint }: { title: string; hint: string }) {
  const navigate = useNavigate();
  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <div className={styles.errorPanelTitleRow}>
          <Icon name="warning" size={16} />
          <h1 className={styles.errorPanelTitle}>{title}</h1>
        </div>
        <p className={styles.errorPanelHint}>{hint}</p>
        <button onClick={() => navigate("/login")} className={styles.primaryButton}>
          前往登录
        </button>
      </div>
    </CenteredCard>
  );
}

function VerificationSentPanel({ email }: { email: string }) {
  const navigate = useNavigate();
  const resend = useResendVerification();
  const [cooldown, setCooldown] = useState(0);

  const handleResend = () => {
    if (cooldown > 0 || resend.isPending) return;
    resend.mutate(email, {
      onSuccess: () => {
        setCooldown(60);
        const timer = setInterval(() => {
          setCooldown((c) => {
            if (c <= 1) {
              clearInterval(timer);
              return 0;
            }
            return c - 1;
          });
        }, 1000);
      },
    });
  };

  return (
    <CenteredCard>
      <Brand />
      <div className={styles.card}>
        <h1 className={styles.title}>验证邮件已发送</h1>
        <p className={styles.description}>
          我们已向 <span className={clsx("mono", styles.inviteEmail)}>{email}</span>{" "}
          发送了一封验证邮件， 请点击邮件中的链接完成验证后再登录。
        </p>
        <button
          type="button"
          onClick={handleResend}
          disabled={cooldown > 0 || resend.isPending}
          className={clsx(
            styles.primaryButton,
            (cooldown > 0 || resend.isPending) && styles.primaryButtonPending,
          )}
        >
          {resend.isPending
            ? "发送中..."
            : cooldown > 0
              ? `重新发送（${cooldown}s）`
              : "重新发送验证邮件"}
        </button>
        <div className={styles.loginPrompt}>
          <button type="button" onClick={() => navigate("/login")} className={styles.link}>
            返回登录
          </button>
        </div>
      </div>
    </CenteredCard>
  );
}

function ErrorBanner({ msg }: { msg: string }) {
  return (
    <div className={styles.errorBanner}>
      <Icon name="warning" size={13} />
      {msg}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className={styles.field}>
      <div className={styles.fieldLabel}>{label}</div>
      {children}
    </label>
  );
}

function Pill({ children }: { children: React.ReactNode }) {
  return <span className={styles.pill}>{children}</span>;
}
