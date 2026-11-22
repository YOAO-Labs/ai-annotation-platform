import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

/** @param {{dst:string, content:string, label:string, detail?:string}} opts */
export function emitGenerated({ dst, content, label, detail }) {
  const check = process.argv.includes("--check");
  if (check) {
    let current = null;
    try {
      current = readFileSync(dst, "utf8");
    } catch {}
    if (current !== content) {
      console.error(
        `[${label}] ✗ ${dst} 与源不一致——源已改但未重新生成。\n` +
          `  运行 \`pnpm docs:gen\`(或对应 generate 脚本)后把生成文件一并提交。`,
      );
      process.exit(1);
    }
    console.log(`[${label}] ✓ 已与源同步${detail ? ` (${detail})` : ""}`);
    return;
  }
  mkdirSync(dirname(dst), { recursive: true });
  writeFileSync(dst, content);
  console.log(`[${label}] wrote${detail ? ` ${detail}` : ""} → ${dst}`);
}
