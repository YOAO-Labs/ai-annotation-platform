#!/usr/bin/env node

import { spawn } from "node:child_process";
import { link, mkdir, readFile, rename, rmdir, unlink, writeFile } from "node:fs/promises";
import { existsSync, readFileSync, realpathSync, unlinkSync } from "node:fs";
import { createServer, connect } from "node:net";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const scriptPath = realpathSync(fileURLToPath(import.meta.url));
const repositoryRoot = realpathSync(resolve(dirname(scriptPath), ".."));
const host = "127.0.0.1";
const defaultApiPort = 8100;
const defaultWebPort = 3100;
const defaultScanLimit = 512;
const defaultLockRoot = join(homedir(), ".cache", "aap-dev-ports");

const delay = (milliseconds) =>
  new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));

function parsePort(raw, option) {
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`${option} 必须是 1–65535 之间的整数`);
  }
  return port;
}

export function parseArguments(argv) {
  const options = {
    apiPort: defaultApiPort,
    webPort: defaultWebPort,
    skipMigrations: false,
    help: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--") continue;
    if (argument === "--help" || argument === "-h") {
      options.help = true;
      continue;
    }
    if (argument === "--skip-migrations") {
      options.skipMigrations = true;
      continue;
    }

    const [name, inlineValue] = argument.split("=", 2);
    if (name === "--api-port" || name === "--web-port") {
      const value = inlineValue ?? argv[index + 1];
      if (inlineValue === undefined) index += 1;
      if (!value || value === "--") throw new Error(`${name} 缺少端口值`);
      const key = name === "--api-port" ? "apiPort" : "webPort";
      options[key] = parsePort(value, name);
      continue;
    }

    throw new Error(`未知参数：${argument}`);
  }

  return options;
}

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

function lockPathFor(lockRoot, port) {
  return join(lockRoot, `${port}.lock`);
}

async function createExclusiveLock(lockPath, owner) {
  const temporaryPath = `${lockPath}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporaryPath, `${JSON.stringify(owner)}\n`, {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });
  try {
    await link(temporaryPath, lockPath);
    return true;
  } catch (error) {
    if (error?.code === "EEXIST") return false;
    throw error;
  } finally {
    await unlink(temporaryPath).catch(() => {});
  }
}

async function discardStaleLock(lockPath) {
  let owner;
  try {
    owner = JSON.parse(await readFile(lockPath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    return false;
  }
  if (processIsAlive(owner?.pid)) return false;
  try {
    await unlink(lockPath);
    return true;
  } catch (error) {
    return error?.code === "ENOENT";
  }
}

export function isPortAvailable(port, listenHost = host) {
  return new Promise((resolveAvailability) => {
    const server = createServer();
    server.unref();
    server.once("error", () => resolveAvailability(false));
    server.listen({ host: listenHost, port, exclusive: true }, () => {
      server.close(() => resolveAvailability(true));
    });
  });
}

export async function reserveAvailablePort({
  startPort,
  lockRoot = defaultLockRoot,
  listenHost = host,
  scanLimit = defaultScanLimit,
  worktree = repositoryRoot,
}) {
  parsePort(startPort, "startPort");
  await mkdir(lockRoot, { recursive: true, mode: 0o700 });

  for (let offset = 0; offset < scanLimit && startPort + offset <= 65_535; offset += 1) {
    const port = startPort + offset;
    const lockPath = lockPathFor(lockRoot, port);
    const token = randomUUID();
    const owner = {
      pid: process.pid,
      token,
      worktree,
      createdAt: new Date().toISOString(),
    };

    let ownsLock = await createExclusiveLock(lockPath, owner);
    if (!ownsLock && (await discardStaleLock(lockPath))) {
      ownsLock = await createExclusiveLock(lockPath, owner);
    }
    if (!ownsLock) continue;

    if (!(await isPortAvailable(port, listenHost))) {
      await unlink(lockPath).catch(() => {});
      continue;
    }

    let released = false;
    const releaseSync = () => {
      if (released || !existsSync(lockPath)) return;
      try {
        const currentOwner = JSON.parse(readFileSync(lockPath, "utf8"));
        if (currentOwner.token === token) unlinkSync(lockPath);
      } catch {
        // A best-effort exit handler must never mask the process exit.
      }
      released = true;
    };
    const release = async () => {
      if (released) return;
      try {
        const currentOwner = JSON.parse(await readFile(lockPath, "utf8"));
        if (currentOwner.token === token) await unlink(lockPath);
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
      } finally {
        released = true;
        await rmdir(lockRoot).catch(() => {});
      }
    };

    return { port, release, releaseSync };
  }

  throw new Error(`从 ${startPort} 开始未找到可用端口（最多检查 ${scanLimit} 个）`);
}

function canConnect(port) {
  return new Promise((resolveConnection) => {
    const socket = connect({ host, port });
    socket.setTimeout(500);
    socket.once("connect", () => {
      socket.destroy();
      resolveConnection(true);
    });
    const unavailable = () => {
      socket.destroy();
      resolveConnection(false);
    };
    socket.once("error", unavailable);
    socket.once("timeout", unavailable);
  });
}

function printHelp() {
  console.log(`用法：pnpm dev:worktree -- [选项]

同时启动当前 checkout 的 API 和 Web，并从指定起始值向上选取未使用端口。

选项：
  --api-port <port>      API 扫描起始端口（默认 ${defaultApiPort}）
  --web-port <port>      Web 扫描起始端口（默认 ${defaultWebPort}）
  --skip-migrations      不在启动 API 前执行 alembic upgrade head
  -h, --help             显示帮助`);
}

function spawnManaged(children, label, command, args, options) {
  const child = spawn(command, args, {
    ...options,
    detached: process.platform !== "win32",
    stdio: "inherit",
  });
  const managed = { child, label, done: false, result: undefined };
  managed.completion = new Promise((resolveCompletion) => {
    const settle = (result) => {
      if (managed.done) return;
      managed.done = true;
      managed.result = { label, ...result };
      children.delete(managed);
      resolveCompletion(managed.result);
    };
    child.once("error", (error) => settle({ code: null, signal: null, error }));
    child.once("exit", (code, signal) => settle({ code, signal, error: null }));
  });
  children.add(managed);
  return managed;
}

function signalManaged(managed, signal) {
  if (managed.done || !managed.child.pid) return;
  try {
    if (process.platform === "win32") managed.child.kill(signal === "SIGKILL" ? "SIGTERM" : signal);
    else process.kill(-managed.child.pid, signal);
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

async function stopChildren(children, signal = "SIGTERM") {
  const running = [...children];
  for (const managed of running) signalManaged(managed, signal);
  await Promise.race([Promise.all(running.map(({ completion }) => completion)), delay(3_000)]);

  const remaining = [...children];
  for (const managed of remaining) signalManaged(managed, "SIGKILL");
  await Promise.all(remaining.map(({ completion }) => completion));
}

async function waitForListening(managed, port, timeoutMilliseconds = 60_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    if (managed.done) {
      const detail = managed.result.error?.message ?? managed.result.signal ?? managed.result.code;
      throw new Error(`${managed.label} 在监听端口前退出：${detail}`);
    }
    if (await canConnect(port)) return;
    await delay(100);
  }
  throw new Error(`${managed.label} 未在 60 秒内监听 ${host}:${port}`);
}

async function waitForHttp(managed, url) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (managed.done) throw new Error(`${managed.label} 在 HTTP 就绪前退出`);
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
      await response.body?.cancel();
      if (response.ok) return;
    } catch {
      // A listening socket is not yet an initialized API/Vite server.
    }
    await delay(100);
  }
  throw new Error(`${managed.label} 未在 60 秒内通过 HTTP 就绪检查`);
}

export async function runDevWorktree(
  argv = process.argv.slice(2),
  { statePath, apiEnvironment, acceptancePath } = {},
) {
  if (!statePath || !apiEnvironment)
    throw new Error("Service supervisor requires worktree runtime preparation");
  const options = parseArguments(argv);
  if (options.help) {
    printHelp();
    return;
  }

  const children = new Set();
  const reservations = [];
  let shutdownRequested = false;
  let shutdownPromise;
  const releaseLocksOnExit = () => {
    for (const reservation of reservations) reservation.releaseSync();
  };
  const requestShutdown = (signal, exitCode) => {
    if (!shutdownRequested) {
      shutdownRequested = true;
      process.exitCode = exitCode;
      shutdownPromise = stopChildren(children, signal);
    }
    return shutdownPromise;
  };
  const onInterrupt = () => void requestShutdown("SIGINT", 130);
  const onTerminate = () => void requestShutdown("SIGTERM", 143);
  process.once("SIGINT", onInterrupt);
  process.once("SIGTERM", onTerminate);
  process.once("exit", releaseLocksOnExit);

  try {
    // The Python runtime owns database preflight/migration and the environment lock.
    const apiReservation = await reserveAvailablePort({
      startPort: options.apiPort,
      worktree: repositoryRoot,
    });
    reservations.push(apiReservation);
    const webReservation = await reserveAvailablePort({
      startPort: options.webPort,
      worktree: repositoryRoot,
    });
    reservations.push(webReservation);
    if (shutdownRequested) return;

    const apiPort = apiReservation.port;
    const webPort = webReservation.port;
    const apiTarget = `http://${host}:${apiPort}`;
    console.log(`[dev:worktree] ${basename(repositoryRoot)} 已分配 API ${apiPort}、Web ${webPort}`);

    const api = spawnManaged(
      children,
      "API",
      join(repositoryRoot, "apps/api/.venv/bin/python"),
      [
        "-m",
        "uvicorn",
        "app.main:app",
        "--reload",
        "--host",
        host,
        "--port",
        String(apiPort),
        "--timeout-graceful-shutdown",
        "3",
        ...(acceptancePath ? ["--log-level", "warning"] : []),
      ],
      {
        cwd: join(repositoryRoot, "apps/api"),
        env: {
          ...apiEnvironment,
          MIGRATION_DATABASE_URL: "",
          FRONTEND_BASE_URL: `http://${host}:${webPort}`,
        },
      },
    );
    await waitForListening(api, apiPort);
    await waitForHttp(api, `${apiTarget}/health/db`);
    if (shutdownRequested) return;

    const web = spawnManaged(
      children,
      "Web",
      "pnpm",
      ["--filter", "@anno/web", "dev", "--host", host, "--port", String(webPort), "--strictPort"],
      {
        cwd: repositoryRoot,
        env: {
          ...process.env,
          API_PROXY_TARGET: apiTarget,
          PORT: String(webPort),
        },
      },
    );
    await waitForListening(web, webPort);
    await waitForHttp(web, `http://${host}:${webPort}`);
    if (shutdownRequested) return;

    const temporary = `${statePath}.${process.pid}.tmp`;
    try {
      await writeFile(temporary, JSON.stringify({ supervisorPid: process.pid, apiPort, webPort }), {
        mode: 0o600,
        flag: "wx",
      });
      await rename(temporary, statePath);
    } finally {
      await unlink(temporary).catch(() => {});
    }

    console.log(`\n[dev:worktree] 已就绪
  Web:      http://${host}:${webPort}
  API docs: ${apiTarget}/docs
  代理:     /api 和 /ws -> ${apiTarget}

按 Ctrl+C 同时停止两个服务。\n`);

    if (acceptancePath) {
      const { guide } = JSON.parse(await readFile(acceptancePath, "utf8"));
      console.log(formatAcceptanceGuide(guide, `http://${host}:${webPort}`));
    }

    const exited = await Promise.race([api.completion, web.completion]);
    if (!shutdownRequested) {
      const detail = exited.error?.message ?? exited.signal ?? exited.code;
      throw new Error(`${exited.label} 意外退出：${detail}`);
    }
    await shutdownPromise;
  } catch (error) {
    if (!shutdownRequested) {
      process.exitCode = 1;
      console.error(`[dev:worktree] ${error.message}`);
      await stopChildren(children);
    }
  } finally {
    process.removeListener("SIGINT", onInterrupt);
    process.removeListener("SIGTERM", onTerminate);
    process.removeListener("exit", releaseLocksOnExit);
    await Promise.all(reservations.map(({ release }) => release()));
    await unlink(statePath).catch(() => {});
  }
}

export function formatAcceptanceGuide(guide, webUrl) {
  return [
    "[dev:worktree] 筛选手动验收（数据保留到显式 reset/destroy）",
    `  登录: ${webUrl}/login`,
    ...guide.accounts.map((account) => `  ${account.role}: ${account.email} / ${account.password}`),
    "",
    ...guide.scenarios.flatMap((scenario) => [
      `  ${scenario.title}`,
      `    ${webUrl}${scenario.path}`,
      `    ${scenario.expected}`,
    ]),
    "",
    "  再次启动同一命令会复用数据；保留验收记录时禁止本环境 exec，切换自动化测试前请显式 reset。",
    "  停止: pnpm dev:worktree -- stop --mode e2e",
    "  重建: 停止后用 doctor 获取确认值，执行 reset --mode e2e --confirm <确认值>，再启动。",
    "",
  ].join("\n");
}

export async function runIsolatedCommand(
  command,
  { apiPort = defaultApiPort, webPort = defaultWebPort } = {},
) {
  const children = new Set();
  const reservations = [];
  const stop = () => void stopChildren(children);
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    reservations.push(await reserveAvailablePort({ startPort: apiPort }));
    reservations.push(await reserveAvailablePort({ startPort: webPort }));
    const child = spawnManaged(children, "隔离命令", command[0], command.slice(1), {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        PLAYWRIGHT_ISOLATED_API_PORT: String(reservations[0].port),
        PLAYWRIGHT_ISOLATED_WEB_PORT: String(reservations[1].port),
      },
    });
    const result = await child.completion;
    if (result.error) throw result.error;
    return result.code ?? 1;
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
    await stopChildren(children);
    await Promise.all(reservations.map(({ release }) => release()));
  }
}

export async function runRuntime(argv = process.argv.slice(2)) {
  const environment = join(repositoryRoot, "apps/api/.venv");
  const localEnvironment = existsSync(environment) && realpathSync(environment) === environment;
  if (!localEnvironment && !argv.some((argument) => argument === "--help" || argument === "-h")) {
    throw new Error("需要当前 checkout 独立的 apps/api/.venv；请先执行 Orca worktree setup");
  }
  const children = new Set();
  const runtime = spawnManaged(
    children,
    "环境管理",
    localEnvironment ? join(environment, "bin/python") : "python3",
    [join(repositoryRoot, "scripts/worktree_runtime.py"), ...argv],
    { cwd: repositoryRoot },
  );
  const interrupt = () => signalManaged(runtime, "SIGINT");
  const terminate = () => signalManaged(runtime, "SIGTERM");
  process.once("SIGINT", interrupt);
  process.once("SIGTERM", terminate);
  try {
    const result = await runtime.completion;
    if (result.error) throw result.error;
    process.exitCode = result.code ?? 1;
  } finally {
    process.removeListener("SIGINT", interrupt);
    process.removeListener("SIGTERM", terminate);
  }
}

if (
  process.argv[1] &&
  existsSync(process.argv[1]) &&
  realpathSync(process.argv[1]) === scriptPath
) {
  try {
    await runRuntime();
  } catch (error) {
    console.error(`[dev:worktree] ${error.message}`);
    process.exitCode = 1;
  }
}
