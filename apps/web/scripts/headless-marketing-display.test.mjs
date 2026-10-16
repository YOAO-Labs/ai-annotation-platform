import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  nvidiaBusId,
  headlessXorgConfig,
  reserveDisplay,
  resolveHeadlessTools,
  withHeadlessDisplay,
} from "./headless-marketing-display.mjs";

function temporary(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "aap-headless-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function fixture(
  t,
  {
    captureCode = 0,
    ready = () => true,
    delay = async () => {},
    launchFailure,
    captureStarted,
  } = {},
) {
  const tmpRoot = temporary(t);
  const launched = [];
  const stopped = [];
  const calls = [];
  const tools = {
    Xorg: "/fake/Xorg",
    xauth: "/fake/xauth",
    xdpyinfo: "/fake/xdpyinfo",
    metacity: "/fake/metacity",
    ffmpeg: "/fake/ffmpeg",
    ffprobe: "/fake/ffprobe",
    busId: "PCI:115:0:0",
  };
  const runtime = {
    tmpRoot,
    xRoot: tmpRoot,
    readyTimeout: 1,
    resolveTools: () => tools,
    run: (command, args, env) => calls.push({ command, args, env }),
    ready,
    delay,
    launch(command, args, env) {
      const child = new EventEmitter();
      Object.assign(child, { pid: launched.length + 100, exitCode: null, signalCode: null });
      launched.push({ command, args, env, child });
      if (command === launchFailure) child.exitCode = 1;
      if (command === "/fake/node") {
        captureStarted?.(child);
        if (captureCode !== null)
          setImmediate(() => {
            child.exitCode = captureCode;
            child.emit("close", captureCode, null);
          });
      }
      return child;
    },
    async stop(child) {
      stopped.push(child.pid);
      child.exitCode ??= 0;
    },
  };
  return { tmpRoot, runtime, launched, stopped, calls };
}

test("headless config validates PCI values and uses an isolated NVIDIA seat with 4K virtual screen", () => {
  assert.equal(nvidiaBusId("00000000:73:00.0"), "PCI:115:0:0");
  assert.equal(nvidiaBusId("0001:0a:01.2"), "PCI:10@1:1:2");
  assert.throws(() => nvidiaBusId('73:0:0"'), /Invalid/);
  assert.throws(() => headlessXorgConfig("PCI:115:0:0", "seat0"), /Invalid/);
  const config = headlessXorgConfig("PCI:115:0:0", "aap-capture-test");
  assert.match(config, /Driver "nvidia"/);
  assert.match(config, /Virtual 3840 2160/);
  assert.match(config, /MatchSeat "aap-capture-test"/);
  assert.match(config, /UseDisplayDevice" "none/);
  assert.match(config, /AutoAddDevices" "false/);
});

test("reservation preserves occupied X sockets, X locks and concurrent wrapper locks", (t) => {
  const tmpRoot = temporary(t);
  fs.mkdirSync(path.join(tmpRoot, ".X11-unix"));
  fs.writeFileSync(path.join(tmpRoot, ".X90-lock"), "existing server");
  fs.writeFileSync(path.join(tmpRoot, ".X11-unix/X91"), "existing socket");
  fs.mkdirSync(path.join(tmpRoot, "aap-headless-display-92.lock"));
  const first = reserveDisplay({ tmpRoot, xRoot: tmpRoot, first: 90, last: 94 });
  const second = reserveDisplay({ tmpRoot, xRoot: tmpRoot, first: 90, last: 94 });
  assert.equal(first.display, ":93");
  assert.equal(second.display, ":94");
  assert.throws(
    () => reserveDisplay({ tmpRoot, xRoot: tmpRoot, first: 90, last: 94 }),
    /No unoccupied/,
  );
  assert.equal(fs.readFileSync(path.join(tmpRoot, ".X90-lock"), "utf8"), "existing server");
});

test("NVENC selection verifies encoding and falls back without replacing the rest of PATH", () => {
  const env = { PATH: "/node/bin:/portable/bin:/usr/bin" };
  const probes = [];
  const run = (command, args) => {
    probes.push({ command, args });
    if (command.endsWith("nvidia-smi")) return "00000000:73:00.0\n";
    if (args.includes("-encoders"))
      return command === "/usr/bin/ffmpeg" ? "h264_nvenc libx264" : "libx264";
    if (args.includes("-devices")) return "x11grab";
    return "";
  };
  const find = (command) => (command.includes("/") ? command : `/portable/bin/${command}`);
  const tools = resolveHeadlessTools(env, run, find);
  assert.equal(tools.ffmpeg, "/usr/bin/ffmpeg");
  assert.equal(tools.ffprobe, "/usr/bin/ffprobe");
  assert.ok(probes.some(({ args }) => args.includes("color=size=256x256:rate=60")));
  assert.equal(env.PATH, "/node/bin:/portable/bin:/usr/bin");
  assert.throws(
    () => resolveHeadlessTools({ ...env, MARKETING_HEADLESS_GPU: "unlisted" }, run, find),
    /must match/,
  );
  assert.throws(
    () => resolveHeadlessTools({ ...env, MARKETING_FFMPEG: "/portable/bin/ffmpeg" }, run, find),
    /No usable NVENC/,
  );
});

test("owned lifecycle preserves environment, creates private auth, propagates capture exit and cleans only owned resources", async (t) => {
  const f = fixture(t, { captureCode: 23 });
  const untouched = path.join(f.tmpRoot, "unrelated");
  fs.writeFileSync(untouched, "keep");
  const env = {
    PATH: "/node/bin:/pnpm/bin:/usr/bin",
    DISPLAY: ":0",
    XAUTHORITY: "/existing/auth",
    TOKEN: "preserved",
  };
  const original = { ...env };
  const result = await withHeadlessDisplay("/fake/node", ["selector.mjs", "--flow", "bbox-draw"], {
    platform: "linux",
    env,
    runtime: f.runtime,
  });
  assert.equal(result, 23);
  assert.deepEqual(env, original);
  assert.equal(f.launched.length, 3);
  const [xorg, wm, capture] = f.launched;
  assert.equal(xorg.args[0], ":90");
  assert.ok(xorg.args.includes("-nolisten"));
  assert.notEqual(xorg.args[xorg.args.indexOf("-seat") + 1], "seat0");
  assert.match(wm.args.join(" "), /--no-composite/);
  assert.equal(capture.env.MARKETING_CAPTURE_DRIVER, "x11grab");
  assert.equal(capture.env.MARKETING_CAPTURE_DISPLAY, ":90");
  assert.equal(capture.env.MARKETING_XAUTHORITY, capture.env.XAUTHORITY);
  assert.equal(capture.env.GSETTINGS_BACKEND, "memory");
  assert.ok(capture.env.PATH.endsWith(env.PATH));
  assert.equal(capture.env.TOKEN, env.TOKEN);
  assert.deepEqual(f.stopped, [capture.child.pid, wm.child.pid, xorg.child.pid]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), ["unrelated"]);
});

test(
  "real child process groups are terminated, including a recorder descendant after its parent exits",
  { skip: process.platform !== "linux" },
  async (t) => {
    const root = temporary(t);
    const binaries = path.join(root, "bin");
    fs.mkdirSync(binaries);
    for (const name of ["Xorg", "metacity"]) {
      fs.writeFileSync(
        path.join(binaries, name),
        `#!${process.execPath}\nimport fs from 'node:fs';\nfs.writeFileSync(process.env.TEST_PID_ROOT+'/${name}.pid',String(process.pid));\nsetInterval(()=>{},1000);\n`,
        { mode: 0o700 },
      );
    }
    const tools = {
      Xorg: path.join(binaries, "Xorg"),
      metacity: path.join(binaries, "metacity"),
      xauth: "/fake/xauth",
      ffmpeg: "/fake/ffmpeg",
      ffprobe: "/fake/ffprobe",
      busId: "PCI:115:0:0",
    };
    const result = await withHeadlessDisplay(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        `
    import {spawn} from 'node:child_process';
    import fs from 'node:fs';
    const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});
    fs.writeFileSync(process.env.TEST_PID_ROOT+'/descendant.pid',String(child.pid));
    setTimeout(()=>process.exit(7),100);
  `,
      ],
      {
        platform: "linux",
        env: { ...process.env, TEST_PID_ROOT: root },
        runtime: {
          tmpRoot: root,
          xRoot: root,
          resolveTools: () => tools,
          run: () => "",
          ready: () => true,
        },
      },
    );
    assert.equal(result, 7);
    for (const name of ["Xorg", "metacity", "descendant"]) {
      const pid = fs.readFileSync(path.join(root, `${name}.pid`), "utf8");
      const status = `/proc/${pid}/status`;
      // An orphan may briefly remain as a reaped-by-init zombie, but cannot execute anymore.
      if (fs.existsSync(status)) assert.match(fs.readFileSync(status, "utf8"), /State:\s+Z/);
    }
    assert.ok(!fs.readdirSync(root).some((name) => name.startsWith("aap-headless-")));
  },
);

test("Xorg failure never launches capture and cleans temporary authentication/reservation", async (t) => {
  const f = fixture(t, { ready: () => false, launchFailure: "/fake/Xorg" });
  await assert.rejects(
    withHeadlessDisplay("/fake/node", [], { platform: "linux", env: {}, runtime: f.runtime }),
    /Xorg exited before readiness/,
  );
  assert.equal(f.launched.length, 1);
  assert.deepEqual(f.stopped, [100]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), []);
});

test("Metacity startup failure cleans Xorg and never starts capture", async (t) => {
  const f = fixture(t, { launchFailure: "/fake/metacity" });
  await assert.rejects(
    withHeadlessDisplay("/fake/node", [], { platform: "linux", env: {}, runtime: f.runtime }),
    /Metacity failed/,
  );
  assert.equal(f.launched.length, 2);
  assert.deepEqual(f.stopped, [101, 100]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), []);
});

test("a child cleanup error preserves the capture failure and still cleans every other resource", async (t) => {
  const f = fixture(t, { launchFailure: "/fake/metacity" });
  f.runtime.stop = async (child) => {
    f.stopped.push(child.pid);
    if (child.pid === 101) throw new Error("simulated stop failure");
  };
  await assert.rejects(
    withHeadlessDisplay("/fake/node", [], { platform: "linux", env: {}, runtime: f.runtime }),
    /Metacity failed/,
  );
  assert.deepEqual(f.stopped, [101, 100]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), []);
});

test("SIGTERM during capture cleans every owned process and removes its handlers", async (t) => {
  const listeners = process.listenerCount("SIGTERM");
  const f = fixture(t, {
    captureCode: null,
    captureStarted: () => setImmediate(() => process.emit("SIGTERM")),
  });
  await assert.rejects(
    withHeadlessDisplay("/fake/node", [], { platform: "linux", env: {}, runtime: f.runtime }),
    /interrupted \(SIGTERM\)/,
  );
  assert.deepEqual(f.stopped, [102, 101, 100]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), []);
  assert.equal(process.listenerCount("SIGTERM"), listeners);
});

test("display crash during capture fails and cleans the capture process", async (t) => {
  let f;
  f = fixture(t, {
    captureCode: null,
    captureStarted: () => setImmediate(() => f.launched[0].child.emit("close", 1)),
  });
  await assert.rejects(
    withHeadlessDisplay("/fake/node", [], { platform: "linux", env: {}, runtime: f.runtime }),
    /ended during capture/,
  );
  assert.deepEqual(f.stopped, [102, 101, 100]);
  assert.deepEqual(fs.readdirSync(f.tmpRoot), []);
});

test("read-only selector previews never probe headless tools or require services", () => {
  const cli = fileURLToPath(new URL("./run-recording-capture.mjs", import.meta.url));
  const env = { ...process.env, PATH: "/missing-headless-tools", SCREENSHOT_DATABASE_URL: "" };
  const list = execFileSync(process.execPath, [cli, "--headless-display", "--list"], {
    env,
    encoding: "utf8",
  });
  assert.match(list, /bbox-draw/);
  if (process.platform === "linux") {
    const preview = execFileSync(
      process.execPath,
      [cli, "--headless-display", "--flow", "bbox-draw", "--plan"],
      { env, encoding: "utf8" },
    );
    assert.equal(JSON.parse(preview).headlessDisplay, true);
    assert.equal(JSON.parse(preview).profile, "marketing");
  }
  assert.throws(
    () =>
      execFileSync(
        process.execPath,
        [
          cli,
          "--headless-display",
          "--flow",
          "bbox-draw",
          "--profile",
          "docs",
          "--validate-only",
          "--plan",
        ],
        { env, stdio: "pipe" },
      ),
    /Linux marketing profile/,
  );
});
