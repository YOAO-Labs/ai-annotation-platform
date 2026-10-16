import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function nvidiaBusId(value) {
  const match = value.match(/^([0-9a-f]{4,8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$/i);
  if (!match) throw new Error(`Invalid NVIDIA PCI bus ID: ${value}`);
  const [, domain, bus, device, slot] = match.map((part, index) =>
    index ? parseInt(part, 16) : part,
  );
  return `PCI:${bus}${domain ? `@${domain}` : ""}:${device}:${slot}`;
}

export function headlessXorgConfig(busId, seat) {
  // Both values are generated/validated internally; never interpolate arbitrary config text.
  if (!/^PCI:\d+(?:@\d+)?:\d+:\d+$/.test(busId) || !/^aap-capture-[\w-]+$/.test(seat)) {
    throw new Error("Invalid headless Xorg device or seat");
  }
  return `Section "ServerLayout"
 Identifier "CaptureLayout"
 Screen 0 "CaptureScreen"
 Option "AutoAddDevices" "false"
 Option "AutoAddGPU" "false"
 Option "AllowEmptyInput" "true"
EndSection
Section "Files"
 ModulePath "/usr/lib/x86_64-linux-gnu/nvidia/xorg"
 ModulePath "/usr/lib/xorg/modules"
EndSection
Section "Device"
 Identifier "CaptureGPU"
 Driver "nvidia"
 BusID "${busId}"
 MatchSeat "${seat}"
 Option "AllowEmptyInitialConfiguration" "true"
 Option "UseDisplayDevice" "none"
EndSection
Section "Screen"
 Identifier "CaptureScreen"
 Device "CaptureGPU"
 DefaultDepth 24
 SubSection "Display"
  Depth 24
  Virtual 3840 2160
 EndSubSection
EndSection
`;
}

export function reserveDisplay({
  tmpRoot = os.tmpdir(),
  xRoot = "/tmp",
  first = 90,
  last = 199,
} = {}) {
  for (let number = first; number <= last; number++) {
    const reservation = path.join(tmpRoot, `aap-headless-display-${number}.lock`);
    const occupied = () =>
      fs.existsSync(path.join(xRoot, `.X${number}-lock`)) ||
      fs.existsSync(path.join(xRoot, ".X11-unix", `X${number}`));
    if (occupied()) continue;
    try {
      fs.mkdirSync(reservation, { mode: 0o700 });
    } catch (error) {
      if (error.code === "EEXIST") continue;
      throw error;
    }
    if (occupied()) {
      fs.rmdirSync(reservation);
      continue;
    }
    return { display: `:${number}`, reservation, number };
  }
  throw new Error("No unoccupied headless X11 display; existing sockets/locks are preserved.");
}

function executable(command, env) {
  const candidates = command.includes("/")
    ? [command]
    : (env.PATH ?? "")
        .split(path.delimiter)
        .filter(Boolean)
        .map((dir) => path.join(dir, command));
  for (const candidate of candidates) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return path.resolve(candidate);
    } catch {
      /* Try the next PATH entry. */
    }
  }
  throw new Error(`Headless capture requires executable ${command}; install it before capture.`);
}

function checked(command, args, env) {
  const result = spawnSync(command, args, { env, encoding: "utf8", timeout: 20_000 });
  if (result.error || result.status !== 0) {
    throw new Error(
      `Headless capture: ${path.basename(command)} failed: ${result.error?.message ?? result.stderr?.trim() ?? result.status}`,
    );
  }
  return result.stdout;
}

export function resolveHeadlessTools(env, run = checked, find = executable) {
  const tools = Object.fromEntries(
    ["xauth", "xdpyinfo", "metacity", "nvidia-smi"].map((name) => [name, find(name, env)]),
  );
  // Bypass Xorg.wrap, which requires a physical console. Never use sudo or change system config.
  tools.Xorg = find("/usr/lib/xorg/Xorg", env);
  const buses = run(tools["nvidia-smi"], ["--query-gpu=pci.bus_id", "--format=csv,noheader"], env)
    .trim()
    .split(/\r?\n/)
    .map((value) => value.trim());
  const bus = env.MARKETING_HEADLESS_GPU ?? buses[0];
  if (!buses.some((value) => value.toLowerCase() === bus.toLowerCase())) {
    throw new Error(
      "MARKETING_HEADLESS_GPU must match an NVIDIA PCI bus ID reported by nvidia-smi.",
    );
  }
  tools.busId = nvidiaBusId(bus);
  const candidates = env.MARKETING_FFMPEG ? [env.MARKETING_FFMPEG] : ["ffmpeg", "/usr/bin/ffmpeg"];
  const errors = [];
  for (const candidate of candidates) {
    try {
      const ffmpeg = find(candidate, env);
      const ffprobe = find(
        env.MARKETING_FFPROBE ?? path.join(path.dirname(ffmpeg), "ffprobe"),
        env,
      );
      const encoders = run(ffmpeg, ["-hide_banner", "-encoders"], env);
      const devices = run(ffmpeg, ["-hide_banner", "-devices"], env);
      if (
        !/\bh264_nvenc\b/.test(encoders) ||
        !/\blibx264\b/.test(encoders) ||
        !/\bx11grab\b/.test(devices)
      ) {
        throw new Error("requires h264_nvenc, libx264, and x11grab");
      }
      run(ffprobe, ["-version"], env);
      run(
        ffmpeg,
        [
          "-v",
          "error",
          "-f",
          "lavfi",
          "-i",
          "color=size=64x64:rate=60",
          "-frames:v",
          "1",
          "-c:v",
          "h264_nvenc",
          "-f",
          "null",
          "-",
        ],
        env,
      );
      return { ...tools, ffmpeg, ffprobe };
    } catch (error) {
      errors.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(`No usable NVENC ffmpeg/ffprobe pair. ${errors.join("; ")}`);
}

function launch(command, args, env, logFd) {
  const child = spawn(command, args, {
    env,
    detached: true,
    stdio: logFd === undefined ? "inherit" : ["ignore", logFd, logFd],
  });
  // Record asynchronous exec errors immediately, even while waiting for display readiness.
  child.once("error", (error) => {
    child.launchError = error;
  });
  return child;
}

function alive(child) {
  return child && !child.launchError && child.exitCode === null && child.signalCode === null;
}

async function stop(child) {
  if (!child?.pid) return;
  const signal = (value) => {
    try {
      process.kill(-child.pid, value);
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  };
  // Detached process groups belong to this wrapper, including recorder/browser descendants.
  signal("SIGTERM");
  const deadline = Date.now() + 3000;
  while (alive(child) && Date.now() < deadline) await delay(50);
  signal("SIGKILL");
}

function displayReady(display, pid, tools, env) {
  try {
    const owner = fs.readFileSync(`/tmp/.X${display.slice(1)}-lock`, "utf8").trim();
    if (Number(owner) !== pid) return false;
    checked(tools.xdpyinfo, ["-display", display], env);
    return true;
  } catch {
    return false;
  }
}

/** Own only this invocation's display, authentication, window manager, and process groups. */
export async function withHeadlessDisplay(
  command,
  args,
  { env = process.env, platform = process.platform, runtime = {} } = {},
) {
  if (platform !== "linux") throw new Error("--headless-display requires Linux NVIDIA X11.");
  const operations = {
    resolveTools: resolveHeadlessTools,
    launch,
    stop,
    run: checked,
    ready: displayReady,
    delay,
    ...runtime,
  };
  const tools = operations.resolveTools(env);
  const root = fs.mkdtempSync(path.join(runtime.tmpRoot ?? os.tmpdir(), "aap-headless-capture-"));
  fs.chmodSync(root, 0o700);
  const children = [];
  const logFds = [];
  let reservation;
  let interrupted;
  let primaryError;
  let rejectInterrupt;
  const interruption = new Promise((_, reject) => {
    rejectInterrupt = reject;
  });
  // A rejection can occur during readiness checks before the final Promise.race.
  interruption.catch(() => {});
  const handlers = Object.fromEntries(
    ["SIGINT", "SIGTERM"].map((signal) => [
      signal,
      () => {
        interrupted = signal;
        rejectInterrupt(new Error(`Headless capture interrupted (${signal})`));
      },
    ]),
  );
  for (const [signal, handler] of Object.entries(handlers)) process.on(signal, handler);
  const start = (name, executablePath, argv, childEnv, log = true) => {
    if (interrupted) throw new Error(`Headless capture interrupted (${interrupted})`);
    const fd = log ? fs.openSync(path.join(root, `${name}.log`), "w", 0o600) : undefined;
    if (fd !== undefined) logFds.push(fd);
    const child = operations.launch(executablePath, argv, childEnv, fd);
    children.push(child);
    return child;
  };
  try {
    reservation = reserveDisplay({ tmpRoot: runtime.tmpRoot, xRoot: runtime.xRoot });
    const { display } = reservation;
    const seat = `aap-capture-${process.pid}-${randomBytes(4).toString("hex")}`;
    const authority = path.join(root, "Xauthority");
    fs.writeFileSync(authority, "", { mode: 0o600 });
    const bin = path.join(root, "bin");
    fs.mkdirSync(bin, { mode: 0o700 });
    fs.symlinkSync(tools.ffmpeg, path.join(bin, "ffmpeg"));
    fs.symlinkSync(tools.ffprobe, path.join(bin, "ffprobe"));
    const childEnv = {
      ...env,
      DISPLAY: display,
      MARKETING_CAPTURE_DISPLAY: display,
      XAUTHORITY: authority,
      MARKETING_XAUTHORITY: authority,
      MARKETING_CAPTURE_DRIVER: "x11grab",
      GSETTINGS_BACKEND: "memory",
      PATH: `${bin}${path.delimiter}${env.PATH ?? ""}`,
    };
    operations.run(
      tools.xauth,
      ["-f", authority, "add", display, ".", randomBytes(16).toString("hex")],
      childEnv,
    );
    const config = path.join(root, "xorg.conf");
    fs.writeFileSync(config, headlessXorgConfig(tools.busId, seat), { mode: 0o600 });
    const configDir = path.join(root, "conf.d");
    fs.mkdirSync(configDir, { mode: 0o700 });
    const xorg = start(
      "xorg-stdout",
      tools.Xorg,
      [
        display,
        "-seat",
        seat,
        "-config",
        config,
        "-configdir",
        configDir,
        "-auth",
        authority,
        "-logfile",
        path.join(root, "Xorg.log"),
        "-nolisten",
        "tcp",
        "-noreset",
        "-isolateDevice",
        tools.busId,
        "-novtswitch",
        "-sharevts",
        "-keeptty",
      ],
      childEnv,
    );
    const deadline = Date.now() + (runtime.readyTimeout ?? 15_000);
    while (!operations.ready(display, xorg.pid, tools, childEnv)) {
      if (interrupted) throw new Error(`Headless capture interrupted (${interrupted})`);
      if (!alive(xorg))
        throw new Error(
          `NVIDIA Xorg exited before readiness: ${xorg.launchError?.message ?? xorg.exitCode ?? xorg.signalCode}. Verify current-user GPU device permissions and driver installation.`,
        );
      if (Date.now() >= deadline)
        throw new Error(
          "NVIDIA Xorg readiness timed out; verify driver and current-user GPU device permissions.",
        );
      await operations.delay(100);
    }
    const wm = start("metacity", tools.metacity, ["--sm-disable", "--no-composite"], childEnv);
    await operations.delay(300);
    if (!alive(wm))
      throw new Error(
        `Metacity failed to start: ${wm.launchError?.message ?? wm.exitCode ?? wm.signalCode}`,
      );
    console.log(
      `[marketing] Owned NVIDIA headless display ${display}, GPU ${tools.busId}; existing capture qualification remains enabled.`,
    );
    const capture = start("capture", command, args, childEnv, false);
    const ended = new Promise((resolve, reject) => {
      capture.once("error", reject);
      capture.once("close", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
    });
    const failed = children.slice(0, -1).map(
      (child) =>
        new Promise((_, reject) => {
          child.once("close", () =>
            reject(new Error("Headless display/window manager ended during capture.")),
          );
        }),
    );
    return await Promise.race([ended, interruption, ...failed]);
  } catch (error) {
    primaryError = error;
    // Keep useful server diagnostics in the error; authentication files are always removed.
    const log = path.join(root, "Xorg.log");
    if (fs.existsSync(log))
      console.error(fs.readFileSync(log, "utf8").split("\n").slice(-25).join("\n"));
    throw error;
  } finally {
    const cleanupErrors = [];
    const cleanup = async (operation) => {
      try {
        await operation();
      } catch (error) {
        cleanupErrors.push(error);
      }
    };
    for (const child of children.reverse()) await cleanup(() => operations.stop(child));
    for (const [signal, handler] of Object.entries(handlers))
      process.removeListener(signal, handler);
    for (const fd of logFds) await cleanup(() => fs.closeSync(fd));
    await cleanup(() => fs.rmSync(root, { recursive: true, force: true }));
    if (reservation) await cleanup(() => fs.rmdirSync(reservation.reservation));
    if (cleanupErrors.length) {
      if (!primaryError) throw new AggregateError(cleanupErrors, "Headless capture cleanup failed");
      console.error(
        `Headless capture cleanup also failed: ${cleanupErrors.map((error) => error.message).join("; ")}`,
      );
    }
  }
}
