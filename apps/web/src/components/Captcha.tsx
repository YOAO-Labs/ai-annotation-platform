import { useEffect, useRef } from "react";
import { getTurnstileSiteKey, renderTurnstile, removeTurnstile } from "@/lib/turnstile";
import styles from "./Captcha.module.css";

/**
 * Cloudflare Turnstile widget.
 *
 * - VITE_TURNSTILE_SITE_KEY 缺省时不渲染、不阻塞表单（dev/CI 透传）。
 * - token 通过 onChange 回调暴露；过期 / 错误时回传 null，由父组件禁用提交。
 */
export interface CaptchaProps {
  onChange: (token: string | null) => void;
  theme?: "light" | "dark" | "auto";
}

export function Captcha({ onChange, theme = "auto" }: CaptchaProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);

  useEffect(() => {
    const sitekey = getTurnstileSiteKey();
    if (!sitekey || !ref.current) return;

    let cancelled = false;
    void renderTurnstile(ref.current, sitekey, {
      theme,
      onToken: (token) => {
        if (!cancelled) onChange(token);
      },
      onError: () => {
        if (!cancelled) onChange(null);
      },
      onExpired: () => {
        if (!cancelled) onChange(null);
      },
    }).then((id) => {
      if (cancelled) {
        if (id) removeTurnstile(id);
      } else {
        widgetIdRef.current = id;
      }
    });

    return () => {
      cancelled = true;
      removeTurnstile(widgetIdRef.current);
      widgetIdRef.current = null;
    };
  }, [onChange, theme]);

  if (!getTurnstileSiteKey()) return null;
  return <div ref={ref} data-testid="captcha-widget" className={styles.widget} />;
}

/** 当且仅当 site key 配置时才需要 token；dev 模式 site key 缺省直接放行。 */
export function isCaptchaRequired(): boolean {
  return getTurnstileSiteKey() !== null;
}
