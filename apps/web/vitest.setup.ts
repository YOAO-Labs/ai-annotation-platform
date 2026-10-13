import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { handleUnhandledRequest } from "./src/test/apiRequestGuard";
import { server } from "./src/mocks/server";

// A loaded CI runner occasionally needs longer than testing-library's default 1s
// waitFor for a react-query resolve → render → interaction chain (for example a
// capabilities request before ProjectDetailPanel's OCR case). 5s is a shared
// safety net for the not-yet-migrated suites, not a substitute for fixing a bad
// mock or an unawaited timer; new integration tests keep waits local to the
// operation they exercise. waitFor still returns as soon as the condition holds.
configure({ asyncUtilTimeout: 5000 });

if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string): MediaQueryList => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// react-konva renders every canvas component as a DOM stand-in carrying
// data-konva / data-testid so existing RTL interaction and prop assertions work.
// Limitation: this verifies component interaction and props only, never real
// canvas rendering — pixel, focus and pointer regressions stay with Playwright.
vi.mock("react-konva", () => import("./src/test/konvaMock"));

// jsdom served from an opaque origin does not provide localStorage /
// sessionStorage, which breaks zustand persist with
// "storage.setItem is not a function". Provide an in-memory implementation.
function createMemoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => {
      map.set(k, String(v));
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
    key: (i: number) => Array.from(map.keys())[i] ?? null,
  };
}
const g = globalThis as unknown as {
  localStorage?: Storage;
  sessionStorage?: Storage;
};
if (!g.localStorage || typeof g.localStorage.setItem !== "function") {
  Object.defineProperty(globalThis, "localStorage", {
    value: createMemoryStorage(),
    writable: true,
    configurable: true,
  });
}
if (!g.sessionStorage || typeof g.sessionStorage.setItem !== "function") {
  Object.defineProperty(globalThis, "sessionStorage", {
    value: createMemoryStorage(),
    writable: true,
    configurable: true,
  });
}
afterEach(() => {
  globalThis.localStorage?.clear();
  globalThis.sessionStorage?.clear();
});

// One MSW server per suite. Override an endpoint with `server.use(http.get(...))`.
// Unhandled target API requests are recorded so an integration suite can fail on
// them via expectNoUnexpectedApiRequests; other traffic keeps a warning.
beforeAll(() => server.listen({ onUnhandledRequest: handleUnhandledRequest }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
