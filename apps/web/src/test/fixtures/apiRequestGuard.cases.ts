import { once } from "node:events";
import { createServer } from "node:http";
import { HttpResponse, http } from "msw";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../mocks/server";

const requests: string[] = [];
const staticPath = "/test-fixture.json?next=/api/v9/missing";
const listener = createServer((request, response) => {
  requests.push(request.url ?? "");
  response.end("loopback");
});
let origin: string;

beforeAll(async () => {
  const listening = once(listener, "listening");
  listener.listen(0, "127.0.0.1");
  await listening;
  const address = listener.address();
  if (!address || typeof address === "string") throw new Error("Expected a TCP listener");
  origin = `http://127.0.0.1:${address.port}`;
});

afterAll(async () => {
  const closed = once(listener, "close");
  listener.close();
  listener.closeAllConnections();
  await closed;
  expect(requests).toEqual([staticPath]);
});

const fetchPath = (path: string) => fetch(`${origin}${path}`);

it("fails the case that caught an unmatched fetch", async () => {
  const result = await fetchPath("/api/v7?detail=missing").catch(() => null);
  expect(result).toBeNull();
});

describe("configured handler", () => {
  beforeEach(() => {
    server.use(http.get(`${origin}/api/v7/scoped`, () => HttpResponse.text("configured")));
  });

  it("passes the next case with an explicit handler", async () => {
    expect(await (await fetchPath("/api/v7/scoped")).text()).toBe("configured");
  });
});

it("fails if a previous case's handler is no longer in scope", async () => {
  const result = await fetchPath("/api/v7/scoped").catch(() => null);
  expect(result).toBeNull();
});

let startLate!: () => void;
const lateGate = new Promise<void>((resolve) => {
  startLate = resolve;
});
let lateResult: Promise<Response | null>;

it("owns a request dispatched during the next case", () => {
  lateResult = lateGate.then(() => fetchPath("/api/v7/late").catch(() => null));
});

it("does not inherit an earlier case's late failure", async () => {
  server.use(http.get(`${origin}/api/v7/late`, () => HttpResponse.text("next case")));
  startLate();
  expect(await lateResult).toBeNull();
  expect(await (await fetchPath("/api/v7/late")).text()).toBe("next case");
});

it("accepts an explicit fetch double", async () => {
  const double = vi.fn<typeof fetch>().mockResolvedValue(new Response("deliberate double"));
  vi.stubGlobal("fetch", double);
  expect(await (await fetchPath("/api/v7/double")).text()).toBe("deliberate double");
  expect(double.mock.calls).toEqual([[`${origin}/api/v7/double`]]);
});

it("restores interception after a fetch double", async () => {
  const result = await fetchPath("/api/v7/after-double").catch(() => null);
  expect(result).toBeNull();
});

it("accepts an explicit fetch spy", async () => {
  const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("deliberate spy"));
  expect(await (await fetchPath("/api/v7/spy")).text()).toBe("deliberate spy");
  expect(spy.mock.calls).toEqual([[`${origin}/api/v7/spy`]]);
});

it("restores interception after a fetch spy", async () => {
  const result = await fetchPath("/api/v7/after-spy").catch(() => null);
  expect(result).toBeNull();
});

it("allows a real nonproduct path even with an API path in its query", async () => {
  expect(await (await fetchPath(staticPath)).text()).toBe("loopback");
});
