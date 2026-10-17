import assert from "node:assert/strict";
import { copyFile, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { parseArguments, reserveAvailablePort } from "./dev-worktree.mjs";
import * as launcher from "./dev-worktree.mjs";

function listen(server, port = 0) {
  return new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen({ host: "127.0.0.1", port }, () => resolveListen(server.address().port));
  });
}

function close(server) {
  return new Promise((resolveClose, rejectClose) => {
    server.close((error) => (error ? rejectClose(error) : resolveClose()));
  });
}

test("parses preferred ports and migration options", () => {
  assert.deepEqual(parseArguments([]), {
    apiPort: 8100,
    webPort: 3100,
    skipMigrations: false,
    help: false,
  });
  assert.deepEqual(
    parseArguments(["--", "--api-port", "8200", "--web-port=3200", "--skip-migrations"]),
    {
      apiPort: 8200,
      webPort: 3200,
      skipMigrations: true,
      help: false,
    },
  );
  assert.throws(() => parseArguments(["--api-port", "0"]), /1–65535/);
  assert.throws(() => parseArguments(["--unknown"]), /未知参数/);
});

test("skips a port already occupied by another process", async () => {
  const lockRoot = await mkdtemp(join(tmpdir(), "aap-dev-port-test-"));
  const blocker = createServer();
  const occupiedPort = await listen(blocker);
  let reservation;
  try {
    reservation = await reserveAvailablePort({
      startPort: occupiedPort,
      lockRoot,
      scanLimit: 32,
      worktree: "/test/occupied",
    });
    assert.notEqual(reservation.port, occupiedPort);
  } finally {
    await reservation?.release();
    await close(blocker);
    await rm(lockRoot, { recursive: true, force: true });
  }
});

test("serializes concurrent worktree reservations and releases the lock", async () => {
  const lockRoot = await mkdtemp(join(tmpdir(), "aap-dev-port-test-"));
  const probe = createServer();
  const startPort = await listen(probe);
  await close(probe);
  let first;
  let second;
  let reused;
  try {
    [first, second] = await Promise.all([
      reserveAvailablePort({ startPort, lockRoot, scanLimit: 32, worktree: "/test/a" }),
      reserveAvailablePort({ startPort, lockRoot, scanLimit: 32, worktree: "/test/b" }),
    ]);
    assert.notEqual(first.port, second.port);

    await first.release();
    reused = await reserveAvailablePort({
      startPort: first.port,
      lockRoot,
      scanLimit: 1,
      worktree: "/test/c",
    });
    assert.equal(reused.port, first.port);
  } finally {
    await first?.release();
    await second?.release();
    await reused?.release();
    await rm(lockRoot, { recursive: true, force: true });
  }
});

test("default port locks coordinate processes with different application temp directories", async () => {
  const directory = await mkdtemp(join(tmpdir(), "aap-port-scope-"));
  const probe = createServer();
  const startPort = await listen(probe);
  await close(probe);
  const first = await reserveAvailablePort({ startPort });
  try {
    const module = new URL("./dev-worktree.mjs", import.meta.url).href;
    const result = spawnSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        `
      import { reserveAvailablePort } from ${JSON.stringify(module)};
      const reservation = await reserveAvailablePort({ startPort: ${first.port} });
      console.log(reservation.port);
      await reservation.release();
    `,
      ],
      {
        encoding: "utf8",
        env: { ...process.env, TMPDIR: directory, TMP: directory, TEMP: directory },
      },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.notEqual(Number(result.stdout.trim()), first.port);
  } finally {
    await first.release();
    await rm(directory, { recursive: true, force: true });
  }
});

test("Playwright keeps owner credentials in setup and runtime credentials in the API", async () => {
  const directory = await mkdtemp(join(tmpdir(), "aap-playwright-roles-"));
  try {
    const module = new URL("../apps/web/playwright.config.ts", import.meta.url).href;
    const typescript = new URL(
      "../apps/web/node_modules/typescript/lib/typescript.js",
      import.meta.url,
    ).href;
    const compiled = join(directory, "playwright.config.mjs");
    await symlink(
      fileURLToPath(new URL("../apps/web/node_modules", import.meta.url)),
      join(directory, "node_modules"),
    );
    const owner = "postgresql+asyncpg://owner:fixture@localhost/aap_fixture_e2e";
    const runtime = owner.replace("owner:", "runtime:");
    const result = spawnSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        `
      import ts from ${JSON.stringify(typescript)};
      import { readFileSync, writeFileSync } from "node:fs";
      const source = readFileSync(new URL(${JSON.stringify(module)}), "utf8");
      writeFileSync(${JSON.stringify(compiled)}, ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
      }).outputText);
      const { default: config } = await import(${JSON.stringify(compiled)});
      console.log(JSON.stringify(config.webServer[0]));
    `,
      ],
      {
        encoding: "utf8",
        env: {
          ...process.env,
          CI: "",
          AAP_WORKTREE_MODE: "e2e",
          DATABASE_URL: runtime,
          PLAYWRIGHT_E2E_DATABASE_URL: owner,
        },
      },
    );
    assert.equal(result.status, 0, result.stderr);
    const api = JSON.parse(result.stdout);
    const log = join(directory, "commands.jsonl");
    await writeFile(
      join(directory, "uv"),
      `#!${process.execPath}
      const { appendFileSync } = require("node:fs");
      appendFileSync(${JSON.stringify(log)}, JSON.stringify({
        args: process.argv.slice(2),
        database: process.env.DATABASE_URL,
        migration: process.env.MIGRATION_DATABASE_URL,
        test: process.env.TEST_DATABASE_URL,
        fixture: process.env.PLAYWRIGHT_E2E_DATABASE_URL,
      }) + "\\n");
    `,
      { mode: 0o700 },
    );
    const launched = spawnSync("sh", ["-c", api.command], {
      encoding: "utf8",
      env: {
        ...process.env,
        ...api.env,
        PATH: `${directory}:${process.env.PATH}`,
        TEST_DATABASE_URL: owner,
        PLAYWRIGHT_E2E_DATABASE_URL: owner,
      },
    });
    assert.equal(launched.status, 0, launched.stderr);
    const [prepare, migrate, server] = (await readFile(log, "utf8"))
      .trim()
      .split("\n")
      .map(JSON.parse);
    assert.equal(prepare.database, owner);
    assert.equal(migrate.migration, owner);
    assert.equal(server.database, runtime);
    assert.equal(server.migration, "");
    assert.equal(server.test, "");
    assert.equal(server.fixture, "");
    assert.equal(api.env.MIGRATION_DATABASE_URL, owner);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("reclaims a lock left by a terminated launcher", async () => {
  const lockRoot = await mkdtemp(join(tmpdir(), "aap-dev-port-test-"));
  const probe = createServer();
  const startPort = await listen(probe);
  await close(probe);
  let reservation;
  try {
    await writeFile(
      join(lockRoot, `${startPort}.lock`),
      `${JSON.stringify({ pid: 2_147_483_647, token: "stale" })}\n`,
    );
    reservation = await reserveAvailablePort({
      startPort,
      lockRoot,
      scanLimit: 1,
      worktree: "/test/recovered",
    });
    assert.equal(reservation.port, startPort);
  } finally {
    await reservation?.release();
    await rm(lockRoot, { recursive: true, force: true });
  }
});

test("isolated exec reserves distinct Playwright ports and preserves exit status", async () => {
  assert.equal(typeof launcher.runIsolatedCommand, "function");
  const directory = await mkdtemp(join(tmpdir(), "aap-command-test-"));
  const output = join(directory, "child.json");
  try {
    const code = await launcher.runIsolatedCommand([
      process.execPath,
      "-e",
      `require('node:fs').writeFileSync(${JSON.stringify(output)}, JSON.stringify({
        api: process.env.PLAYWRIGHT_ISOLATED_API_PORT,
        web: process.env.PLAYWRIGHT_ISOLATED_WEB_PORT
      })); process.exitCode = 4;`,
    ]);
    assert.equal(code, 4);
    const child = JSON.parse(await readFile(output, "utf8"));
    assert.notEqual(child.api, child.web);
    assert.ok(Number(child.api) > 0);
    assert.ok(Number(child.web) > 0);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("service supervisor cannot bypass runtime preparation", async () => {
  assert.equal(typeof launcher.runRuntime, "function");
  await assert.rejects(launcher.runDevWorktree([], {}), /runtime/);
});

test("acceptance instructions use the actual reserved URL and keep credentials scoped to fixtures", () => {
  const output = launcher.formatAcceptanceGuide(
    {
      accounts: [{ role: "管理员", email: "admin@e2e.test", password: "Test1234" }],
      scenarios: [
        { title: "分页", path: "/projects/fixture/data-manager?lens=objects", expected: "101" },
      ],
    },
    "http://127.0.0.1:3199",
  );
  assert.match(output, /http:\/\/127\.0\.0\.1:3199\/login/);
  assert.match(output, /http:\/\/127\.0\.0\.1:3199\/projects\/fixture\/data-manager\?lens=objects/);
  assert.match(output, /admin@e2e.test \/ Test1234/);
  assert.match(output, /数据保留/);
});

test("CLI help works before dependency setup without touching services", async () => {
  const directory = await mkdtemp(join(tmpdir(), "aap-cli-bootstrap-"));
  try {
    await mkdir(join(directory, "scripts"));
    for (const file of ["dev-worktree.mjs", "worktree_runtime.py", "worktree_env.py"]) {
      await copyFile(new URL(file, import.meta.url), join(directory, "scripts", file));
    }
    const result = spawnSync(
      process.execPath,
      [join(directory, "scripts/dev-worktree.mjs"), "--help"],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /doctor/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("CLI follows a symlinked invocation path instead of silently doing nothing", async () => {
  const directory = await mkdtemp(join(tmpdir(), "aap-cli-alias-"));
  try {
    const alias = join(directory, "launcher.mjs");
    await symlink(fileURLToPath(new URL("./dev-worktree.mjs", import.meta.url)), alias);
    const result = spawnSync(process.execPath, [alias, "--help"], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /doctor/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
