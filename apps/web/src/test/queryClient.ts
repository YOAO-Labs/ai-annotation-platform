import { QueryClient } from "@tanstack/react-query";

/**
 * A fresh, retry-free QueryClient for integration tests.
 *
 * Integration suites build one client per test so cache entries and in-flight
 * requests cannot leak between tests through a module-level singleton.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}
