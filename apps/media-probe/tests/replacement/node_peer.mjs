import { spawn } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import { createWriteStream, mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { once } from "node:events";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const DEFAULT_MAX_FILE_BYTES = 1 << 30;
const FFPROBE_TIMEOUT_MS = 30_000;
const MAX_NDJSON_LINE_BYTES = 64 * 1024;

function ratioOfDouble(value) {
  if (!Number.isFinite(value)) {
    throw new RangeError("Cannot round a non-finite number");
  }
  if (value === 0) return [0n, 1n];

  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, value, false);
  const bits = view.getBigUint64(0, false);
  const sign = bits >> 63n ? -1n : 1n;
  const exponent = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & ((1n << 52n) - 1n);
  const significand = exponent === 0 ? fraction : (1n << 52n) | fraction;
  const exponent2 = exponent === 0 ? -1074 : exponent - 1023 - 52;
  if (exponent2 >= 0) return [sign * (significand << BigInt(exponent2)), 1n];
  return [sign * significand, 1n << BigInt(-exponent2)];
}

function roundRatioTiesToEven(numerator, denominator) {
  const sign = numerator < 0n ? -1n : 1n;
  const magnitude = numerator < 0n ? -numerator : numerator;
  let quotient = magnitude / denominator;
  const remainder = magnitude % denominator;
  const midpoint = remainder * 2n - denominator;
  if (midpoint > 0n || (midpoint === 0n && quotient % 2n === 1n)) quotient += 1n;
  return sign * quotient;
}

// Python uses ties-to-even; JavaScript Math.round rounds midpoints toward +infinity.
export function pythonRoundInteger(value) {
  const [numerator, denominator] = ratioOfDouble(value);
  const rounded = roundRatioTiesToEven(numerator, denominator);
  if (rounded > BigInt(MAX_SAFE_INTEGER) || rounded < BigInt(-MAX_SAFE_INTEGER)) {
    throw new RangeError("Rounded integer exceeds the JSON safe integer range");
  }
  return Number(rounded);
}

export function pythonRoundDigits(value, digits) {
  const [numerator, denominator] = ratioOfDouble(value);
  const scale = 10n ** BigInt(digits);
  const rounded = roundRatioTiesToEven(numerator * scale, denominator);
  return Number(rounded) / Number(scale);
}

class ProbeError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function positiveIntegerEnv(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  if (!/^[1-9][0-9]*$/.test(raw)) throw new Error(`${name} must be a positive integer`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) throw new Error(`${name} exceeds the safe integer range`);
  return value;
}

const temporaryDirectories = new Set();
const probeChildren = new Set();
let shuttingDown = false;
let expectedToken;
let maxFileBytes;
let server;

function jsonReply(response, status, body) {
  if (response.writableEnded || response.destroyed) return;
  response.writeHead(status, {
    "content-type": "application/json",
    connection: "close",
  });
  response.end(JSON.stringify(body));
}

function fail(status, code, message) {
  throw new ProbeError(status, code, message);
}

function authorize(request) {
  const [scheme, value, extra] = (request.headers.authorization ?? "").split(" ");
  const presented = Buffer.from(value ?? "", "utf8");
  if (
    scheme?.toLowerCase() !== "bearer" ||
    !value ||
    extra !== undefined ||
    presented.length !== expectedToken.length ||
    !timingSafeEqual(presented, expectedToken)
  ) {
    fail(401, "invalid_request", "A valid bearer token is required");
  }
}

function removeTemporaryDirectory(directory) {
  temporaryDirectories.delete(directory);
  rmSync(directory, { recursive: true, force: true });
}

async function saveUpload(request, response) {
  const contentType = (request.headers["content-type"] ?? "").split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/octet-stream") {
    fail(400, "invalid_request", "Content-Type must be application/octet-stream");
  }
  const rawLength = request.headers["content-length"];
  if (rawLength !== undefined) {
    if (!/^(0|[1-9][0-9]*)$/.test(rawLength)) {
      fail(400, "invalid_request", "Content-Length must be a non-negative integer");
    }
    if (BigInt(rawLength) > BigInt(maxFileBytes)) {
      fail(413, "file_too_large", "Uploaded file exceeds the configured limit");
    }
  }

  const directory = mkdtempSync(path.join(tmpdir(), "aap-media-probe-"));
  temporaryDirectories.add(directory);
  const inputPath = path.join(directory, "input.media");
  const output = createWriteStream(inputPath, { flags: "wx" });
  let size = 0;
  try {
    await once(output, "open");
    for await (const chunk of request) {
      size += chunk.length;
      if (size > maxFileBytes) {
        output.destroy();
        if (!response.writableEnded) {
          response.shouldKeepAlive = false;
          jsonReply(response, 413, {
            code: "file_too_large",
            message: "Uploaded file exceeds the configured limit",
          });
        }
        request.socket.destroySoon();
        throw new ProbeError(413, "file_too_large", "Uploaded file exceeds the configured limit");
      }
      if (!output.write(chunk)) await once(output, "drain");
    }
    output.end();
    await once(output, "finish");
    if (size === 0) fail(400, "invalid_request", "The uploaded file is empty");
    return { directory, inputPath };
  } catch (error) {
    output.destroy();
    if (!output.closed) await new Promise((resolve) => output.once("close", resolve));
    removeTemporaryDirectory(directory);
    throw error;
  }
}

function parseNumber(value) {
  if (value === undefined || value === null || value === "N/A") return null;
  const raw = String(value).trim();
  if (
    !raw ||
    !/^[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|Infinity|inf(?:inity)?|NaN)$/i.test(raw)
  ) {
    return null;
  }
  if (/^[+-]?nan$/i.test(raw)) return Number.NaN;
  if (/^[+-]?inf(?:inity)?$/i.test(raw)) {
    return raw.startsWith("-") ? Number.NEGATIVE_INFINITY : Number.POSITIVE_INFINITY;
  }
  return Number(raw);
}

function parseRatio(value) {
  if (!value) return null;
  const slash = value.indexOf("/");
  if (slash >= 0) {
    const numerator = parseNumber(value.slice(0, slash));
    const denominator = parseNumber(value.slice(slash + 1));
    if (numerator === null || denominator === null || denominator === 0) return null;
    return numerator / denominator;
  }
  return parseNumber(value);
}

function parseStrictInteger(value) {
  if (value === undefined || value === null || value === "N/A") return null;
  if (typeof value === "number") {
    if (!Number.isInteger(value)) return null;
    if (!Number.isSafeInteger(value))
      throw new Error("ffprobe integer exceeds JSON safe integer range");
    return value;
  }
  if (!/^[+-]?[0-9]+$/.test(String(value))) return null;
  const parsed = BigInt(value);
  if (parsed > BigInt(MAX_SAFE_INTEGER) || parsed < BigInt(-MAX_SAFE_INTEGER)) {
    throw new Error("ffprobe integer exceeds JSON safe integer range");
  }
  return Number(parsed);
}

function safeInteger(value) {
  if (!Number.isSafeInteger(value)) throw new Error("Probe result exceeds JSON safe integer range");
  return value;
}

function nonNegativeSafeInteger(value) {
  const integer = safeInteger(value);
  if (integer < 0) throw new Error("Probe result contains a negative non-negative integer");
  return integer;
}

function metadata(payload) {
  const stream = (payload.streams ?? []).find((item) => item.codec_type === "video");
  if (!stream) throw new Error("ffprobe did not return a video stream");
  const format = payload.format ?? {};
  const averageFps = parseRatio(stream.avg_frame_rate);
  const fps =
    averageFps !== null && averageFps !== 0 ? averageFps : parseRatio(stream.r_frame_rate);
  let duration = null;
  for (const value of [stream.duration, format.duration]) {
    if (value === undefined || value === null) continue;
    const parsed = parseNumber(value);
    if (parsed !== null) {
      duration = parsed;
      break;
    }
  }
  let frameCount = null;
  if (stream.nb_frames !== undefined && stream.nb_frames !== "N/A") {
    frameCount = parseStrictInteger(stream.nb_frames);
  }
  if (frameCount === null && fps && duration) {
    frameCount = Math.max(1, pythonRoundInteger(fps * duration));
  }
  const width = stream.width === undefined ? null : parseStrictInteger(stream.width);
  const height = stream.height === undefined ? null : parseStrictInteger(stream.height);
  const fpsResult = fps && Number.isFinite(fps) ? pythonRoundDigits(fps, 3) : null;
  if (fpsResult !== null && fpsResult < 0)
    throw new Error("ffprobe returned a negative frame rate");
  if (
    stream.codec_name !== undefined &&
    stream.codec_name !== null &&
    typeof stream.codec_name !== "string"
  ) {
    throw new Error("ffprobe returned an invalid codec name");
  }
  return {
    duration_ms: duration === null ? null : safeInteger(pythonRoundInteger(duration * 1000)),
    fps: fpsResult,
    frame_count: frameCount === null ? null : nonNegativeSafeInteger(frameCount),
    width: width === null ? null : nonNegativeSafeInteger(width),
    height: height === null ? null : nonNegativeSafeInteger(height),
    codec: stream.codec_name === undefined ? null : stream.codec_name,
  };
}

function frameTimeMs(frame) {
  for (const key of ["best_effort_timestamp_time", "pkt_pts_time", "pts_time", "pkt_dts_time"]) {
    const seconds = parseNumber(frame[key]);
    if (seconds !== null && Number.isFinite(seconds)) {
      return safeInteger(pythonRoundInteger(seconds * 1000));
    }
  }
  return null;
}

function frames(payload) {
  const output = [];
  for (const frame of payload.frames ?? []) {
    if (frame.media_type !== undefined && frame.media_type !== null && frame.media_type !== "video")
      continue;
    const pts = frameTimeMs(frame);
    if (pts === null) continue;
    if (
      frame.pict_type !== undefined &&
      frame.pict_type !== null &&
      typeof frame.pict_type !== "string"
    ) {
      throw new Error("ffprobe returned an invalid picture type");
    }
    const offset = parseStrictInteger(frame.pkt_pos);
    output.push({
      frame_index: nonNegativeSafeInteger(output.length),
      pts_ms: pts,
      is_keyframe: Boolean(parseStrictInteger(frame.key_frame) || 0),
      pict_type: frame.pict_type ?? null,
      byte_offset: offset === null ? null : nonNegativeSafeInteger(offset),
    });
  }
  return output;
}

function packets(payload, startFrame) {
  const valid = [];
  for (const packet of payload.packets ?? []) {
    const position = parseStrictInteger(packet.pos);
    if (
      position === null ||
      packet.pts_time === undefined ||
      packet.pts_time === null ||
      packet.pts_time === "N/A"
    )
      continue;
    const pts = Number(packet.pts_time);
    if (!Number.isFinite(pts)) throw new Error("ffprobe returned an invalid packet timestamp");
    const rawDuration = packet.duration_time;
    const duration =
      rawDuration === undefined || rawDuration === null || rawDuration === "N/A"
        ? 0
        : Number(rawDuration);
    if (!Number.isFinite(duration)) throw new Error("ffprobe returned an invalid packet duration");
    const size = parseStrictInteger(packet.size) || 0;
    valid.push({
      pts_ms: safeInteger(pythonRoundInteger(pts * 1000)),
      duration_ms: safeInteger(pythonRoundInteger(duration * 1000)),
      is_keyframe: String(packet.flags ?? "").includes("K"),
      size_bytes: nonNegativeSafeInteger(size),
      offset_in_chunk: nonNegativeSafeInteger(position),
    });
  }
  const order = valid
    .map((_, index) => index)
    .sort((left, right) => valid[left].pts_ms - valid[right].pts_ms || left - right);
  const rank = new Array(valid.length);
  order.forEach((index, frameRank) => {
    rank[index] = frameRank;
  });
  return valid.map((packet, index) => ({
    ...packet,
    frame_index: nonNegativeSafeInteger(startFrame + rank[index]),
  }));
}

function runFfprobe(args, inputPath) {
  return new Promise((resolve, reject) => {
    const child = spawn("ffprobe", [...args, inputPath], { stdio: ["ignore", "pipe", "pipe"] });
    probeChildren.add(child);
    const stdout = [];
    const stderr = [];
    let stderrBytes = 0;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, FFPROBE_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => {
      if (stderrBytes >= 8192) return;
      const rest = chunk.subarray(0, 8192 - stderrBytes);
      stderr.push(rest);
      stderrBytes += rest.length;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      probeChildren.delete(child);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      probeChildren.delete(child);
      if (timedOut) {
        reject(new ProbeError(504, "probe_timeout", "ffprobe exceeded its 30 second time limit"));
      } else if (code !== 0) {
        reject(
          new ProbeError(
            422,
            "probe_failed",
            Buffer.concat(stderr).toString("utf8").trim() || "ffprobe failed",
          ),
        );
      } else {
        try {
          resolve(JSON.parse(Buffer.concat(stdout).toString("utf8") || "{}"));
        } catch (error) {
          reject(error);
        }
      }
    });
  });
}

const metadataArgs = [
  "-v",
  "error",
  "-protocol_whitelist",
  "file,pipe",
  "-select_streams",
  "v:0",
  "-show_entries",
  "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
  "-show_entries",
  "format=duration",
  "-of",
  "json",
];
const framesArgs = [
  "-v",
  "error",
  "-protocol_whitelist",
  "file,pipe",
  "-select_streams",
  "v:0",
  "-show_frames",
  "-show_entries",
  "frame=media_type,key_frame,pict_type,best_effort_timestamp_time,pkt_pts_time,pts_time,pkt_dts_time,pkt_pos",
  "-of",
  "json",
];
const packetsArgs = [
  "-v",
  "error",
  "-protocol_whitelist",
  "file,pipe",
  "-select_streams",
  "v:0",
  "-show_packets",
  "-show_entries",
  "packet=pts_time,dts_time,duration_time,size,pos,flags",
  "-of",
  "json",
];

async function runOperation(request, response, operation, startFrame = 0) {
  const upload = await saveUpload(request, response);
  try {
    let args = metadataArgs;
    if (operation === "frames") args = framesArgs;
    if (operation === "packets") args = packetsArgs;
    const payload = await runFfprobe(args, upload.inputPath);
    if (operation === "metadata") {
      const result = metadata(payload);
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(result));
      return;
    }
    const rows = operation === "frames" ? frames(payload) : packets(payload, startFrame);
    const kind = operation === "frames" ? "frame" : "packet";
    const digest = createHash("sha256");
    response.writeHead(200, { "content-type": "application/x-ndjson; charset=utf-8" });
    for (const data of rows) {
      const line = Buffer.from(`${JSON.stringify({ type: kind, data })}\n`, "utf8");
      if (line.length > MAX_NDJSON_LINE_BYTES) {
        response.destroy();
        throw new ProbeError(422, "probe_failed", "A probe result exceeded the record size limit");
      }
      digest.update(line);
      if (!response.write(line)) await once(response, "drain");
    }
    const completion = Buffer.from(
      `${JSON.stringify({
        type: "complete",
        count: nonNegativeSafeInteger(rows.length),
        sha256: digest.digest("hex"),
      })}\n`,
      "utf8",
    );
    if (completion.length > MAX_NDJSON_LINE_BYTES) {
      response.destroy();
      throw new ProbeError(422, "probe_failed", "A probe result exceeded the record size limit");
    }
    response.end(completion);
  } finally {
    removeTemporaryDirectory(upload.directory);
  }
}

function createPeerServer() {
  return createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    try {
      if (request.method === "GET" && url.pathname === "/health" && url.search === "") {
        jsonReply(response, 200, { status: "ok" });
        return;
      }
      authorize(request);
      const routes = new Map([
        ["/v1/metadata", "metadata"],
        ["/v1/frames", "frames"],
        ["/v1/packets", "packets"],
      ]);
      const operation = routes.get(url.pathname);
      if (request.method !== "POST" || !operation) {
        fail(404, "invalid_request", "Unknown media probe route");
      }
      let startFrame = 0;
      if (operation === "packets") {
        const values = url.searchParams.getAll("start_frame");
        if (values.length !== 1 || !/^(0|[1-9][0-9]*)$/.test(values[0])) {
          fail(400, "invalid_request", "start_frame must be a non-negative safe integer");
        }
        startFrame = Number(values[0]);
        if (!Number.isSafeInteger(startFrame) || startFrame < 0) {
          fail(400, "invalid_request", "start_frame must be a non-negative safe integer");
        }
      }
      await runOperation(request, response, operation, startFrame);
    } catch (error) {
      if (response.writableEnded || response.destroyed) return;
      if (error instanceof ProbeError) {
        jsonReply(response, error.status, { code: error.code, message: error.message });
        return;
      }
      jsonReply(response, 422, {
        code: "probe_failed",
        message: "The uploaded media could not be probed",
      });
    }
  });
}

function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of probeChildren) child.kill("SIGTERM");
  server?.close(() => process.exit(0));
  setTimeout(() => {
    for (const child of probeChildren) child.kill("SIGKILL");
    for (const directory of temporaryDirectories) removeTemporaryDirectory(directory);
    process.exit(0);
  }, 1000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const token = process.env.MEDIA_PROBE_TOKEN;
  if (!token || token.length < 32 || !/^[\x21-\x7e]+$/.test(token)) {
    throw new Error("MEDIA_PROBE_TOKEN must contain at least 32 non-whitespace ASCII characters");
  }
  expectedToken = Buffer.from(token, "ascii");
  maxFileBytes = positiveIntegerEnv("MEDIA_PROBE_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES);
  const rawPort = process.env.MEDIA_PROBE_PORT ?? "8010";
  if (!/^(0|[1-9][0-9]*)$/.test(rawPort)) throw new Error("MEDIA_PROBE_PORT must be a valid port");
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 0 || port > 65535)
    throw new Error("MEDIA_PROBE_PORT must be a valid port");
  server = createPeerServer();
  server.listen(port, "127.0.0.1", () => {
    const address = server.address();
    if (!address || typeof address === "string")
      throw new Error("Could not bind loopback listener");
    process.stdout.write(`${JSON.stringify({ port: address.port })}\n`);
  });
}
