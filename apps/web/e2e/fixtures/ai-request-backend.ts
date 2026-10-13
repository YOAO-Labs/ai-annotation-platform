import type { APIRequestContext, APIResponse } from "@playwright/test";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { networkInterfaces } from "node:os";

type JsonObject = Record<string, unknown>;

export interface AiRequestRecord {
  /** One entry per model HTTP call, recorded before any hold or injected failure. */
  tasks: Array<JsonObject & { id: string }>;
  context: JsonObject;
  receivedAt: number;
  responseStatus?: number;
}

export interface AiRequestBackend {
  url: string;
  requests: AiRequestRecord[];
  /** Hold current and subsequent predictions until release(); other routes stay live. */
  hold(): void;
  /** Fail the next count arriving predictions; configure before submitting a job. */
  failNext(count?: number): void;
  /** Apply to new predictions until explicitly changed; release() only clears hold. */
  failAll(enabled: boolean): void;
  release(): void;
  /** Reuse the backend from the owned seed fixture; creates no global registry or service pool. */
  attach(
    request: APIRequestContext,
    options: {
      apiBase: string;
      projectId: string;
      token: string;
      defaultBackendId: string;
    },
  ): Promise<() => Promise<void>>;
  /** Call after jobs settle and attach's detach callback; releases holds and closes sockets. */
  close(): Promise<void>;
}

const classes = [{ index: 0, name: "car" }];
const defaultSetup = {
  name: "E2 AI request fixture",
  version: "1",
  protocol_version: "2.1",
  compat_protocol_versions: ["2.0"],
  model_version: "e2-ai-request",
  is_interactive: false,
  infra: "onnx",
  models: [
    {
      id: "e2-detect",
      display_name: "E2 detector",
      task: "detection",
      model_family: "yolo",
      infra: "onnx",
      composition: "atom",
      is_interactive: false,
      supported_prompts: ["none"],
      supported_inputs: ["full_image"],
      default_input_type: "full_image",
      supported_geometric_outputs: ["bbox"],
      output_attribute_types: ["select"],
      output_attribute_schema: [
        { key: "color", label: "颜色", type: "select", options: ["blue", "red"] },
      ],
      resource_profile: { device: "cpu", batchable: true },
      supported_variants: [
        {
          key: "size",
          title: "档位",
          variants: [
            { value: "small", label: "小" },
            { value: "large", label: "大" },
          ],
        },
      ],
      default_variants: { size: "small" },
      default_thresholds: { confidence: 0.25 },
      params: {
        type: "object",
        properties: {
          confidence: {
            type: "number",
            title: "置信度阈值",
            minimum: 0,
            maximum: 1,
            default: 0.25,
            "x-platform-role": "confidence",
          },
        },
      },
      classes,
    },
  ],
};

function advertisedHost(): string {
  const host =
    process.env.PLAYWRIGHT_ML_FIXTURE_HOST?.trim() ||
    Object.values(networkInterfaces())
      .flat()
      .find((address) => address?.family === "IPv4" && !address.internal)?.address;
  if (!host) {
    throw new Error("Set PLAYWRIGHT_ML_FIXTURE_HOST to an IPv4 host reachable by API and Celery");
  }
  const parsed = new URL(`http://${host}`);
  if (
    parsed.hostname !== host ||
    parsed.port ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    host === "localhost" ||
    host === "0.0.0.0" ||
    host.startsWith("127.")
  ) {
    throw new Error("PLAYWRIGHT_ML_FIXTURE_HOST must be a non-loopback hostname without a port");
  }
  return host;
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(body));
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(request: IncomingMessage): Promise<JsonObject> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > 1024 * 1024) throw new Error("Fixture expects image URLs, not inline image data");
    chunks.push(bytes);
  }
  const body: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!isObject(body)) throw new Error("Prediction body must be an object");
  return body;
}

async function checked(response: APIResponse): Promise<APIResponse> {
  if (!response.ok()) {
    throw new Error(`Fixture backend API failed (${response.status()}): ${await response.text()}`);
  }
  return response;
}

/**
 * Real model HTTP transport for image preannotation. Business preannotation/job
 * APIs, Celery and prediction persistence remain real. Use one instance per case.
 * batch_predict catches HTTP failures as failed predictions; its max_retries alone
 * does not retry. failAll(true) also makes repeated submissions deterministic.
 */
export async function startAiRequestBackend(
  options: { secondary?: boolean } = {},
): Promise<AiRequestBackend> {
  const setup = options.secondary
    ? {
        ...defaultSetup,
        models: defaultSetup.models.map((model) => ({
          ...model,
          supported_inputs: ["full_image", "crop"],
          supported_prompts: ["text"],
        })),
      }
    : defaultSetup;
  const host = advertisedHost();
  const requests: AiRequestRecord[] = [];
  let gate: { promise: Promise<void>; resolve: () => void } | undefined;
  let failuresRemaining = 0;
  let failEveryRequest = false;
  let closed = false;
  let closing: Promise<void> | undefined;
  let attached = false;

  const release = () => {
    const previous = gate;
    gate = undefined;
    previous?.resolve();
  };
  const assertOpen = () => {
    if (closed) throw new Error("AI request fixture is closed");
  };

  const server = createServer((request, response) => {
    void (async () => {
      const path = new URL(request.url ?? "/", "http://fixture").pathname;
      if (request.method === "GET" && path === "/health") {
        sendJson(response, 200, { ok: true, model_version: setup.model_version });
        return;
      }
      if (request.method === "GET" && path === "/setup") {
        sendJson(response, 200, setup);
        return;
      }
      if (request.method === "GET" && path === "/classes") {
        sendJson(response, 200, { classes });
        return;
      }
      if (request.method !== "POST" || path !== "/predict") {
        sendJson(response, 404, { detail: "Unknown model fixture route" });
        return;
      }

      const body = await readJson(request);
      const singleTask = !Array.isArray(body.tasks) || body.tasks.length === 0;
      const tasks = singleTask ? [body.task] : body.tasks;
      if (
        !Array.isArray(tasks) ||
        !tasks.length ||
        !tasks.every((task) => isObject(task) && typeof task.id === "string")
      ) {
        throw new Error("Prediction requires tasks[].id or task.id");
      }
      const entry: AiRequestRecord = {
        tasks: structuredClone(tasks) as AiRequestRecord["tasks"],
        context: structuredClone(isObject(body.context) ? body.context : {}),
        receivedAt: Date.now(),
      };
      requests.push(entry);
      // Capture the gate and outcome at entry. A later release()+hold() or a new
      // failure mode cannot transfer this request into the next operation.
      const pendingGate = gate;
      const shouldFail = failEveryRequest || failuresRemaining > 0;
      if (failuresRemaining > 0) failuresRemaining -= 1;
      await pendingGate?.promise;
      if (shouldFail || closed) {
        entry.responseStatus = 503;
        sendJson(response, 503, {
          detail: { error_code: "model_unavailable", message: "E2 injected model failure" },
        });
        return;
      }
      const result = {
        result: [
          {
            type: "rectanglelabels",
            value: { x: 12, y: 15, width: 25, height: 30, rectanglelabels: ["car"] },
            attributes: { color: "blue" },
            score: 0.9,
          },
        ],
        score: 0.9,
        model_version: setup.model_version,
        inference_time_ms: 5,
      };
      entry.responseStatus = 200;
      sendJson(
        response,
        200,
        singleTask
          ? result
          : { results: entry.tasks.map((task) => ({ task: task.id, ...result })) },
      );
    })().catch((error: unknown) => {
      sendJson(response, 400, {
        detail: error instanceof Error ? error.message : "Invalid fixture request",
      });
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "0.0.0.0", () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("AI fixture did not bind a TCP port");
  }
  const url = `http://${host}:${address.port}`;

  return {
    url,
    requests,
    hold() {
      assertOpen();
      if (gate) return;
      let resolve!: () => void;
      const promise = new Promise<void>((done) => {
        resolve = done;
      });
      gate = { promise, resolve };
    },
    failNext(count = 1) {
      assertOpen();
      if (!Number.isSafeInteger(count) || count < 1) {
        throw new Error("failNext requires a positive integer");
      }
      failuresRemaining += count;
    },
    failAll(enabled) {
      assertOpen();
      failEveryRequest = enabled;
    },
    release,
    async attach(request, { apiBase, projectId, token, defaultBackendId }) {
      assertOpen();
      if (attached) throw new Error("Detach the current seed backend before attaching again");
      attached = true;
      const endpoint = `${apiBase.replace(/\/$/, "")}/api/v1/projects/${projectId}/ml-backends/${defaultBackendId}`;
      const headers = { Authorization: `Bearer ${token}` };
      let restore: JsonObject | undefined;
      let detached = false;
      const detach = async () => {
        if (detached) return;
        if (restore) await checked(await request.put(endpoint, { headers, data: restore }));
        detached = true;
        attached = false;
      };
      try {
        const snapshot = (await (
          await checked(await request.get(endpoint, { headers }))
        ).json()) as {
          name: string;
          url: string;
          is_interactive: boolean;
          auth_method: string;
          extra_params: JsonObject;
        };
        // This helper owns only a disposable seed backend. Tokens are deliberately
        // never read or replaced. Cached health is disposable and resets with seed.
        if (snapshot.extra_params.e2e_mock !== true || snapshot.auth_method !== "none") {
          throw new Error("attach requires the unauthenticated backend returned by seed.owned()");
        }
        restore = {
          name: snapshot.name,
          url: snapshot.url,
          is_interactive: snapshot.is_interactive,
          extra_params: snapshot.extra_params,
        };
        await checked(
          await request.put(endpoint, {
            headers,
            data: { name: setup.name, url, is_interactive: false, extra_params: {} },
          }),
        );
        const health = (await (
          await checked(await request.post(`${endpoint}/health`, { headers }))
        ).json()) as { status: string };
        if (health.status !== "ok") {
          throw new Error(`Model fixture is unreachable from the API: ${health.status}`);
        }
        return detach;
      } catch (error) {
        try {
          await detach();
        } catch (cleanupError) {
          throw new AggregateError([error, cleanupError], "Fixture attach and restoration failed");
        }
        throw error;
      }
    },
    close() {
      closing ??= new Promise<void>((resolve, reject) => {
        closed = true;
        release();
        server.close((error) => (error ? reject(error) : resolve()));
        // Also closes incomplete uploads and abandoned keep-alive clients.
        server.closeAllConnections();
      });
      return closing;
    },
  };
}
