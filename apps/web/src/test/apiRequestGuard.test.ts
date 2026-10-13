import { describe, expect, it } from "vitest";

import { expectNoUnexpectedApiRequests, handleUnhandledRequest } from "./apiRequestGuard";

const print = { warning: () => {}, error: () => {} };

describe("unexpected API request guard", () => {
  it("fails when a target API request reached the network with no handler", () => {
    handleUnhandledRequest(new Request("http://localhost/api/v1/projects/p1/access"), print);
    expect(() => expectNoUnexpectedApiRequests()).toThrow(/GET \/api\/v1\/projects\/p1\/access/);
  });

  it("ignores non-target resources, the explicit allowlist boundary", () => {
    handleUnhandledRequest(new Request("http://localhost/avatars/pixel/manifest.json"), print);
    expect(() => expectNoUnexpectedApiRequests()).not.toThrow();
  });

  it("consumes a recorded failure so the next test starts clean", () => {
    handleUnhandledRequest(new Request("http://localhost/api/v1/health"), print);
    expect(() => expectNoUnexpectedApiRequests()).toThrow();
    expect(() => expectNoUnexpectedApiRequests()).not.toThrow();
  });
});
