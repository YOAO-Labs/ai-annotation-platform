import type { Plugin } from "vite";

const PLACEHOLDER = "__CSP_NONCE__";

export function cspNoncePlugin(): Plugin {
  return {
    name: "vite-plugin-csp-nonce",
    enforce: "post",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        let out = html.replace(
          /<script\b(?![^>]*\bnonce=)([^>]*)>/g,
          `<script$1 nonce="${PLACEHOLDER}">`,
        );
        out = out.replace(/<style\b(?![^>]*\bnonce=)([^>]*)>/g, `<style$1 nonce="${PLACEHOLDER}">`);
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
