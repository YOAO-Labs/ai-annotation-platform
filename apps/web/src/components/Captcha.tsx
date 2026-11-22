import { useEffect, useRef } from "react";
import { getTurnstileSiteKey, renderTurnstile, removeTurnstile } from "@/lib/turnstile";
import styles from "./Captcha.module.css";

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

export function isCaptchaRequired(): boolean {
  return getTurnstileSiteKey() !== null;
}
