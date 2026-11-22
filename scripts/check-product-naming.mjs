import { execFileSync } from "node:child_process";

const rules = [
  [
    /(?:\b(?:command|entrypoint):\s*\[?\s*["']?|^\s*(?:\$\s+)?(?:exec\s+)?)(?:aap-(?:jobs|identity|inference|labeling|workspace|assets|video|audit|diagnostics|insights|plugin)(?:-[\w-]+)?)(?=[\s\]"',=]|$)/,
    "use the installed yoao-* executable",
  ],
  [
    /^\s*(?:from|import)\s+aap(?:\b|_(?:sdk|inference_plugin|backend_runtime|jobs|identity|labeling|workspace|assets|video|audit|diagnostics|insights)\b)/,
    "import the published yoao package",
  ],
  [
    /^\s*(?:\$\s+)?(?:aap\s+(?:projects|tasks|plugins|login|tui)\b|(?:uv\s+)?pip\s+install\s+aap-sdk\b)/,
    "use the yoao command and yoao-sdk distribution",
  ],
  [
    /\b(?:session|db|first)\.info\[\s*["']aap_|^\s*_[A-Z_]+\s*=\s*["']aap_\w*(?:pending|listeners|reconciliations|stages|baselines)/,
    "use a domain name for new in-process state",
  ],
  [
    /["'`>](?:AI 标注平台|AAP 标注平台|ML Backend not found)["'`<]/,
    "use YOAO or the current model-service wording",
  ],
];
const active =
  /^(?:README\.md$|DEV\.md$|docker-compose[^/]*\.ya?ml$|(?:apps|packages|infra|scripts|docs-site)\/)/;
const preserved =
  /(?:^|\/)(?:alembic|sql|vendor|third_party|fixtures|changelogs?|adr|archive|generated|public)(?:\/|$)|(?:\.lock|\.snapshot\.json|\.generated\.\w+)$/;

export function violations(diff) {
  const errors = [];
  let file = "",
    line = 0,
    remaining = 0;
  for (const row of diff.split("\n")) {
    if (row.startsWith("diff --git ")) {
      file = "";
      remaining = 0;
    } else if (!remaining && row.startsWith("+++ b/")) file = row.slice(6);
    else if (row.startsWith("@@ ")) {
      const hunk = /\+(\d+)(?:,(\d+))? @@/.exec(row);
      line = Number(hunk[1]);
      remaining = Number(hunk[2] ?? 1);
    } else if (remaining && (row[0] === "+" || row[0] === " ")) {
      if (
        row[0] === "+" &&
        active.test(file) &&
        !preserved.test(file) &&
        file !== "scripts/check-product-naming.test.mjs"
      ) {
        for (const [pattern, message] of rules)
          if (pattern.test(row.slice(1))) errors.push(`${file}:${line}: ${message}`);
      }
      line++;
      remaining--;
    }
  }
  return errors;
}

function main(args) {
  const range =
    args.length === 1 && args[0] === "--staged"
      ? ["--cached"]
      : args.length === 2 && args[0] === "--base" && /^[\da-f]{7,64}$/i.test(args[1])
        ? [args[1], "HEAD"]
        : null;
  if (!range) throw new Error("Usage: check-product-naming.mjs --staged | --base <commit>");
  const diff = execFileSync(
    "git",
    [
      "-c",
      "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--unified=0",
      "--diff-filter=ACMR",
      ...range,
      "--",
      "README.md",
      "DEV.md",
      "docker-compose*.yml",
      "apps",
      "packages",
      "infra",
      "scripts",
      "docs-site",
      ":(exclude,glob)**/alembic/**",
      ":(exclude,glob)**/fixtures/**",
      ":(exclude,glob)**/*.lock",
      ":(exclude,glob)**/*.snapshot.json",
      ":(exclude,glob)**/public/**",
    ],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
  );
  const errors = violations(diff);
  if (errors.length) {
    console.error(errors.join("\n"));
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.filename === process.argv[1]) main(process.argv.slice(2));
