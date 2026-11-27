import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const planner = fileURLToPath(new URL("./plan-affected-pr.mjs", import.meta.url));
const project = "apps/web/src/pages/Projects/";
const table = `${project}TrainingRunTable.tsx`;
const panelTest = `${project}ProjectTrainingPanel.test.tsx`;
const chart = `${project}TrainingDatasetCharts.tsx`;
const chartTest = `${project}TrainingDatasetCharts.test.tsx`;
const summary = "apps/api/app/services/audit_summary.py";
const summaryTest = "apps/api/tests/test_audit_summary.py";
const baseline = {
  "README.md": "# Project\n\n```sh\npnpm docs:dev\n```\n",
  "DEV.md": "# Development\n",
  [table]:
    "export function TrainingRunTable({ version }: { version: string }) { return <td>{version}</td>; }\n",
  [panelTest]: 'it("shows the training list", () => {});\n',
  [chart]: `export function Chart({ totalMembers, known, unknown }: { totalMembers?: number; known: number; unknown: number }) {
  const total = known + unknown;
  return <span>{total ? total.toFixed(1) : "0.0"}</span>;
}\n`,
  [chartTest]: `describe("chart", () => {
  it("reports the denominator", () => {
    expect(screen.getByText("2 张 · 40.0%")).toBeVisible();
  });
});\n`,
  [summary]: `def build_monthly_summary(
    month: str, month_start, month_end, rows
):
    for row in rows:
        is_error = int(row.status_family) in (4, 5)
    return [offset for offset in range((month_end - month_start).days)]
`,
  [summaryTest]: `def test_local_summary_keeps_exact_counts_empty_dimensions_and_tie_order():
    assert len(summary.daily) == days
`,
};

function fixture(t, files) {
  const root = mkdtempSync(join(tmpdir(), "affected-pr-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const git = (...args) =>
    execFileSync("git", args, {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }).trim();
  const write = (path, text) => {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  };
  const setFiles = (entries) => {
    for (const [path, text] of Object.entries(entries)) write(path, text);
  };
  const commit = () => {
    git("add", "-A");
    git("commit", "--allow-empty", "-qm", "fixture");
    return git("rev-parse", "HEAD");
  };
  git("init", "-q");
  git("config", "core.hooksPath", "/dev/null");
  git("config", "user.name", "Fixture");
  git("config", "user.email", "fixture@example.invalid");
  git("config", "commit.gpgsign", "false");
  setFiles(files);
  const base = commit();
  const cli = (...args) =>
    spawnSync(process.execPath, [planner, ...args], { cwd: root, encoding: "utf8" });
  const plan = (baseSHA, testedSHA) => {
    const { status, stdout, stderr } = cli(baseSHA, testedSHA);
    assert.equal(status, 0, stderr);
    const { reason, ...checks } = JSON.parse(stdout);
    assert.equal(typeof reason, "string");
    assert.ok(reason.length);
    return checks;
  };
  function changed(edits) {
    setFiles(edits);
    return plan(base, commit());
  }
  return { root, git, write, commit, base, cli, plan, changed };
}

const affected = (docs, web = [], api = []) => ({ scope: "affected", docs, web, api });
const full = { scope: "full", docs: false, web: [], api: [] };
const dayRange = "for offset in range((month_end - month_start).days)";
const changedSummary = baseline[summary].replace(dayRange, `${dayRange.slice(0, -1)} - 1)`);
const withoutVersion = baseline[table].replace("<td>{version}</td>", "<td>—</td>");
const directPercentage = baseline[chart].replace(
  'total ? total.toFixed(1) : "0.0"',
  "total.toFixed(1)",
);
const addedAuditTest = `\n\ndef test_error_totals_exclude_redirects():
    rows = [SimpleNamespace(day=date(2026, 10, 1), action="read", target_type="task",
                            actor_role="admin", status_family=family, event_count=1)
            for family in (3, 4, 5)]
    result = build_monthly_summary(month="2026-10", month_start=date(2026, 10, 1),
                                   month_end=date(2026, 11, 1), materialized_through=None, rows=rows)
    assert result.totals.error_count == 2
`;

test("passive repository guides, plans, ADRs and roadmap notes select just documentation", (t) => {
  const r = fixture(t, { "README.md": baseline["README.md"], "DEV.md": baseline["DEV.md"] });
  assert.deepEqual(
    r.changed({
      "README.md": `${baseline["README.md"]}\nA plain setup note.\n`,
      "DEV.md": `${baseline["DEV.md"]}\nSee [the plan](docs/plans/1234567890_example.md).\n`,
      "docs/plans/1234567890_example.md": "# Plan\n",
      "docs/adr/0181-example.md": "# Decision\n",
      "ROADMAP/archive/0.10.x.md": "# Roadmap\n",
    }),
    affected(true),
  );
});

test("code blocks, active Markdown and unreviewed documentation roots stay full", (t) => {
  const r = fixture(t, { "README.md": baseline["README.md"] });
  for (const text of [
    baseline["README.md"].replace("pnpm docs:dev", "pnpm dev:web"),
    "---\nlayout: page\n---\n# Title\n",
    "<<< @/snippet.ts\n",
    "<!--@include: ./other.md-->\n",
    "<script setup>\nimport App from './App.vue'\n</script>\n",
    "{{ x }}\n",
    "::: demo\ncode\n:::\n",
    "```python exec\nprint(1)\n```\n",
    "# Guide\n.. include:: example.rst\n",
    "# Binary\0data\n",
  ])
    assert.deepEqual(r.changed({ "README.md": text }), full, text.slice(-100));
});

test("reviewed changes select each exact test; one owner unions tests and accompanying docs", (t) => {
  const r = fixture(t, {
    [table]: baseline[table],
    [chart]: baseline[chart],
    [panelTest]: baseline[panelTest],
    [chartTest]: baseline[chartTest],
  });
  assert.deepEqual(r.changed({ [table]: withoutVersion }), affected(false, [panelTest]));
  assert.deepEqual(
    r.changed({ [table]: baseline[table], [chart]: directPercentage }),
    affected(false, [chartTest]),
  );
  assert.deepEqual(
    r.changed({
      [table]: withoutVersion,
      [chart]: baseline[chart].replace("const total = known + unknown;", "const total = known;"),
      "docs/plans/pilot.md": "# Chart percentages\n",
    }),
    affected(true, [panelTest, chartTest]),
  );
});

test("a new component assertion runs its own file; skip, weakening, and broad panel test edits do not qualify", (t) => {
  const r = fixture(t, { [chartTest]: baseline[chartTest], [panelTest]: baseline[panelTest] });
  const visible = '    expect(screen.getByText("2 张 · 40.0%")).toBeVisible();';
  assert.deepEqual(
    r.changed({
      [chartTest]: baseline[chartTest].replace(
        visible,
        `${visible}\n    expect(screen.getByText("another expected label")).toBeVisible();`,
      ),
    }),
    affected(false, [chartTest]),
  );
  for (const change of [
    baseline[chartTest].replace(visible, ""),
    baseline[chartTest].replace('it("', 'it.only("'),
    baseline[chartTest].replace(visible, `${visible}\n    vi.resetAllMocks();`),
  ]) {
    assert.deepEqual(r.changed({ [chartTest]: change }), full);
  }
  assert.deepEqual(
    r.changed({ [chartTest]: baseline[chartTest], [panelTest]: `${baseline[panelTest]}\n` }),
    full,
  );
});

test("monthly calculation and a new distinct, fixture-free audit case each run the audit file", (t) => {
  const r = fixture(t, { [summary]: baseline[summary], [summaryTest]: baseline[summaryTest] });
  assert.deepEqual(r.changed({ [summary]: changedSummary }), affected(false, [], [summaryTest]));
  assert.deepEqual(
    r.changed({
      [summary]: baseline[summary],
      [summaryTest]: baseline[summaryTest] + addedAuditTest,
    }),
    affected(false, [], [summaryTest]),
  );
  assert.deepEqual(
    r.changed({ [summary]: changedSummary, "docs/adr/README.md": "# Monthly totals\n" }),
    affected(true, [], [summaryTest]),
  );
});

test("a path match cannot approve a dependency, contract, decorator or second owner", (t) => {
  const r = fixture(t, baseline);
  for (const [path, text] of [
    [chart, `import "./extra";\n${directPercentage}`],
    [chart, baseline[chart].replace("totalMembers?: number", "totalMembers: number")],
    [
      chart,
      baseline[chart].replace(
        "  const total = known + unknown;",
        '  const total = await import("./lookup");',
      ),
    ],
    [table, `${withoutVersion}\nexport { secret } from "./shared";\n`],
    [summary, `from app.db import session\n${changedSummary}`],
    [summary, changedSummary.replace("month: str,", "month: object,")],
    [
      summary,
      changedSummary.replace(
        "def build_monthly_summary",
        "@load_remote\ndef build_monthly_summary",
      ),
    ],
    [
      summary,
      changedSummary.replace(
        "        is_error = int(",
        "        from shared import int\n        is_error = int(",
      ),
    ],
  ]) {
    assert.deepEqual(r.changed({ ...baseline, [path]: text }), full, path);
  }
  assert.deepEqual(
    r.changed({ ...baseline, [summary]: changedSummary, [chart]: directPercentage }),
    full,
  );
});

test("audit test additions cannot replace a case or add fixtures, decorators, imports or collection hooks", (t) => {
  const r = fixture(t, { [summaryTest]: baseline[summaryTest] });
  for (const addition of [
    addedAuditTest.replace(
      "test_error_totals_exclude_redirects",
      "test_local_summary_keeps_exact_counts_empty_dimensions_and_tie_order",
    ),
    addedAuditTest.replace("():", "(database):"),
    addedAuditTest.replace("def test_", "@pytest.mark.skip\ndef test_"),
    addedAuditTest.replace("    rows", "    import external_service\n    rows"),
    addedAuditTest.replace("    rows", "    client.connect()\n    rows"),
    "\npytest_plugins = ['shared']\n",
    "\npytestmark = pytest.mark.skip\n",
  ])
    assert.deepEqual(r.changed({ [summaryTest]: baseline[summaryTest] + addition }), full);
  assert.deepEqual(r.changed({ [summaryTest]: "def test_only():\n    assert True\n" }), full);
  assert.deepEqual(
    r.changed({
      [summaryTest]:
        baseline[summaryTest].replace("assert len(summary.daily) == days", "assert True") +
        addedAuditTest,
    }),
    full,
  );
});

test("unreviewed code, CI, docs-site, locks, auth, migrations, shared fixtures and unusual paths use full CI", (t) => {
  const r = fixture(t, { "DEV.md": "# Guide\n" });
  for (const path of [
    "scripts/test-suites.json",
    "scripts/plan-affected-pr.mjs",
    ".github/workflows/ci.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs-site/guide.md",
    "docs/plans/example.ts",
    "docs/plans/example.py.md",
    "docs/plans/.tools/example.md",
    "pnpm-lock.yaml",
    "apps/api/app/api/v1/audit_logs.py",
    "apps/api/alembic/versions/0001_first.py",
    "apps/web/src/api/auth.ts",
    "apps/web/e2e/fixtures/shared.ts",
    "apps/api/tests/conftest.py",
    "apps/web/src/pages/Projects/NewChart.tsx",
    "docs/adr/normal\nname.md",
  ]) {
    assert.deepEqual(r.changed({ [path]: "new\n" }), full, path);
    rmSync(join(r.root, path));
  }
});

test("renaming either way, deleting even a document, symlinks and executable bits require full CI", (t) => {
  const initial = {
    "docs/plans/old.md": "# Plan\n",
    [summary]: baseline[summary],
    [summaryTest]: baseline[summaryTest],
  };
  const r = fixture(t, initial);
  const move = (from, to) => renameSync(join(r.root, from), join(r.root, to));
  move("docs/plans/old.md", "docs/plans/new.md");
  assert.deepEqual(r.plan(r.base, r.commit()), full);
  move("docs/plans/new.md", "docs/plans/old.md");
  move(summaryTest, "docs/plans/tests.md");
  assert.deepEqual(r.plan(r.base, r.commit()), full);
  move("docs/plans/tests.md", summaryTest);
  rmSync(join(r.root, "docs/plans/old.md"));
  assert.deepEqual(r.plan(r.base, r.commit()), full);
  symlinkSync("untracked.md", join(r.root, "docs/plans/old.md"));
  assert.deepEqual(r.plan(r.base, r.commit()), full);
  rmSync(join(r.root, "docs/plans/old.md"));
  r.write("docs/plans/old.md", "# Plan\n");
  chmodSync(join(r.root, "docs/plans/old.md"), 0o755);
  assert.deepEqual(r.plan(r.base, r.commit()), full);
  chmodSync(join(r.root, "docs/plans/old.md"), 0o644);
  rmSync(join(r.root, summaryTest));
  assert.deepEqual(r.changed({ [summary]: changedSummary }), full);
});

test("routing uses committed blobs even when checkout files differ, and needs a real test target", (t) => {
  const r = fixture(t, { [chart]: baseline[chart], [chartTest]: baseline[chartTest] });
  r.write(chart, `import './shared';\n${directPercentage}`);
  const tested = r.commit();
  r.write(chart, directPercentage);
  assert.deepEqual(r.plan(r.base, tested), full);
  const missing = fixture(t, { [chart]: baseline[chart] });
  assert.deepEqual(missing.changed({ [chart]: directPercentage }), full);
  const newSource = fixture(t, { [chartTest]: baseline[chartTest] });
  assert.deepEqual(newSource.changed({ [chart]: directPercentage }), full);
});

test("unavailable or empty diff is full; an unverified tested commit is a planning error", (t) => {
  const r = fixture(t, { "DEV.md": "# Base\n" });
  const tested = r.commit();
  for (const base of [r.base, "absent", "0".repeat(40), r.git("rev-parse", "HEAD^{tree}"), ""]) {
    assert.deepEqual(r.plan(base, tested), full);
  }
  for (const wrong of [r.base, tested.slice(0, 8), "HEAD", "0".repeat(40)]) {
    const result = r.cli(r.base, wrong);
    assert.notEqual(result.status, 0);
    assert.equal(result.stdout, "");
  }
});
