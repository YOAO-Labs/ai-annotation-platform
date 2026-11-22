import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import os from "node:os";

const platformNames = {
  darwin: "macOS",
  win32: "Windows",
  linux: "Linux",
};

function run(command, args, timeout = 1_500) {
  try {
    return execFileSync(command, args, {
      encoding: "utf8",
      timeout,
      stdio: ["ignore", "pipe", "ignore"],
      windowsHide: true,
    }).trim();
  } catch {
    return "";
  }
}

function read(path) {
  try {
    return readFileSync(path, "utf8").trim();
  } catch {
    return "";
  }
}

function gib(bytes) {
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function asArray(value) {
  return Array.isArray(value) ? value : value ? [value] : [];
}

function macHardware() {
  const raw = run("system_profiler", ["-json", "SPHardwareDataType", "SPDisplaysDataType"], 3_000);
  if (!raw) return {};

  try {
    const data = JSON.parse(raw);
    const hardware = data.SPHardwareDataType?.[0] ?? {};
    return {
      chip: hardware.chip || hardware.chip_type || hardware.cpu_type,
      gpu: asArray(data.SPDisplaysDataType)
        .map((display) => display.sppci_model)
        .filter(Boolean),
      memory: hardware.physical_memory,
      model: hardware.machine_name || hardware.model_name,
    };
  } catch {
    return {};
  }
}

function windowsHardware() {
  const raw = run(
    "powershell.exe",
    [
      "-NoProfile",
      "-NonInteractive",
      "-Command",
      "$ErrorActionPreference='SilentlyContinue'; Get-CimInstance Win32_VideoController | Select-Object Name | ConvertTo-Json -Compress",
    ],
    2_500,
  );
  if (!raw) return {};

  try {
    return {
      gpu: asArray(JSON.parse(raw))
        .map((gpu) => gpu.Name)
        .filter(Boolean),
    };
  } catch {
    return {};
  }
}

function linuxHardware() {
  const nvidia = run("nvidia-smi", ["--query-gpu=name,memory.total", "--format=csv,noheader"]);
  const gpu = nvidia
    ? nvidia
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
    : run("lspci", ["-nn"])
        .split("\n")
        .filter((line) => /(?:VGA|3D|Display)/i.test(line))
        .map((line) => line.replace(/^\S+\s+[^:]+:\s*/, "").trim())
        .filter(Boolean);

  return {
    gpu,
    model: [read("/sys/class/dmi/id/sys_vendor"), read("/sys/class/dmi/id/product_name")]
      .filter(Boolean)
      .join(" "),
  };
}

function collectHardware() {
  const platform = os.platform();
  const platformHardware =
    platform === "darwin"
      ? macHardware()
      : platform === "win32"
        ? windowsHardware()
        : platform === "linux"
          ? linuxHardware()
          : {};
  const cpu = os.cpus();

  return {
    arch: os.arch(),
    cpu: platformHardware.chip || cpu[0]?.model || "unknown",
    cores: cpu.length,
    freeMemory: gib(os.freemem()),
    gpu: platformHardware.gpu?.join(", ") || "unknown",
    model: platformHardware.model || "unknown",
    platform: platformNames[platform] || platform,
    release: os.release(),
    totalMemory: platformHardware.memory || gib(os.totalmem()),
  };
}

function contextFor(event) {
  const hardware = collectHardware();
  return [
    "Current host hardware (captured at session start; use when hardware matters):",
    `- Platform: ${hardware.platform} ${hardware.release} (${hardware.arch})`,
    `- Machine: ${hardware.model}`,
    `- CPU: ${hardware.cpu} (${hardware.cores} logical cores)`,
    `- Memory: ${hardware.freeMemory} free / ${hardware.totalMemory} total`,
    `- GPU: ${hardware.gpu}`,
    `- Session source: ${event.source || "unknown"}`,
  ].join("\n");
}

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  let event = {};
  try {
    event = JSON.parse(input || "{}");
  } catch {}

  process.stdout.write(
    JSON.stringify({
      continue: true,
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: contextFor(event),
      },
    }),
  );
});
