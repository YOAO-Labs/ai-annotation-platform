#!/usr/bin/env node

import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const TARGET_ORIGIN = "https://yoao-labs.github.io/YOAO-Docs";
const LEGACY_PREFIXES = ["/ai-annotation-platform", "/YOAO"];

export function computeRedirectUrl(pathname, search = "", hash = "") {
  let subpath = pathname || "/";
  for (const prefix of LEGACY_PREFIXES) {
    if (subpath === prefix || subpath.startsWith(`${prefix}/`)) {
      subpath = subpath.slice(prefix.length) || "/";
      break;
    }
  }
  if (!subpath.startsWith("/")) subpath = `/${subpath}`;
  return `${TARGET_ORIGIN}${subpath}${search || ""}${hash || ""}`;
}

export function renderRedirectHtml() {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>YOAO 文档站已迁移</title>
  <meta http-equiv="refresh" content="0; url=${TARGET_ORIGIN}/">
  <link rel="canonical" href="${TARGET_ORIGIN}/">
  <script>
    (function () {
      var target = ${JSON.stringify(TARGET_ORIGIN)};
      var prefixes = ${JSON.stringify(LEGACY_PREFIXES)};
      var path = window.location.pathname || "/";
      for (var i = 0; i < prefixes.length; i++) {
        var prefix = prefixes[i];
        if (path === prefix || path.indexOf(prefix + "/") === 0) {
          path = path.slice(prefix.length) || "/";
          break;
        }
      }
      if (path.charAt(0) !== "/") path = "/" + path;
      window.location.replace(target + path + (window.location.search || "") + (window.location.hash || ""));
    })();
  </script>
</head>
<body>
  <p>YOAO 文档站已迁移至 <a href="${TARGET_ORIGIN}/">${TARGET_ORIGIN}/</a>。</p>
</body>
</html>
`;
}

export function buildRedirectBundle(outDir) {
  const absOut = resolve(outDir);
  mkdirSync(absOut, { recursive: true });
  const html = renderRedirectHtml();
  writeFileSync(resolve(absOut, "index.html"), html, "utf8");
  writeFileSync(resolve(absOut, "404.html"), html, "utf8");
  writeFileSync(resolve(absOut, ".nojekyll"), "", "utf8");
  return absOut;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outDir = process.argv[2] || ".tmp/docs-redirect";
  const built = buildRedirectBundle(outDir);
  console.log(`[build-docs-redirect] wrote static redirect bundle → ${built}`);
}
