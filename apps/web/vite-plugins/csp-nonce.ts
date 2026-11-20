import type { Plugin } from "vite";

const PLACEHOLDER = "__CSP_NONCE__";

/**
 * CSP nonce 占位符注入。
 *
 * build 时给 index.html 中所有 `<script>` / `<style>` 标签加
 * `nonce="__CSP_NONCE__"`，并插入 `<meta name="csp-nonce" content="__CSP_NONCE__">`。
 * 运行时由 Nginx sub_filter
 * 把占位符替换为 per-request 值（`$request_id`），与同请求 CSP header 中的
 * `nonce-XXX` 保持一致。
 *
 * 配套：
 * - infra/docker/nginx.conf 启用 sub_filter
 * - apps/api/app/middleware/security_headers.py CSP script-src/style-src 不再含 'unsafe-inline'
 * - apps/web/src/lib/turnstile.ts 动态注入 script 时读取 meta 设 nonce
 *
 * dev 模式（vite serve）下没有 Nginx，浏览器看到的 nonce 仍是 placeholder 字面量。
 * 这在 dev 不成问题：CSP middleware 也只在 production 注册（main.py 显式 gating）。
 */
export function cspNoncePlugin(): Plugin {
  return {
    name: "vite-plugin-csp-nonce",
    enforce: "post",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        // 给所有 <script ...> / <style ...> 加 nonce 属性 (避免重复加)
        let out = html.replace(
          /<script\b(?![^>]*\bnonce=)([^>]*)>/g,
          `<script$1 nonce="${PLACEHOLDER}">`,
        );
        out = out.replace(/<style\b(?![^>]*\bnonce=)([^>]*)>/g, `<style$1 nonce="${PLACEHOLDER}">`);
        // 在 <head> 内注入 csp-nonce meta（供 turnstile.ts 等动态 script 注入读取）
        if (!/name=["']csp-nonce["']/.test(out)) {
          out = out.replace(
            /<head>/i,
            `<head>\n    <meta name="csp-nonce" content="${PLACEHOLDER}" />`,
          );
        }
        return out;
      },
    },
  };
}
