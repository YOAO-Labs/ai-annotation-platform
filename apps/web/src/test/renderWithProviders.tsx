import { QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement, ReactNode } from "react";

import { createTestQueryClient } from "./queryClient";

export interface RenderWithProvidersOptions {
  initialEntries?: string[];
  /**
   * Probe nodes (location/history readers, navigation buttons) that must share
   * the router and query context with the page under test.
   */
  children?: ReactNode;
}

/**
 * Small provider fixture for integration tests: a fresh QueryClient plus a
 * MemoryRouter around one page. It deliberately holds no page state, access
 * data or handlers — callers describe those at the API boundary with MSW.
 */
export function renderWithProviders(
  ui: ReactElement,
  { initialEntries = ["/"], children }: RenderWithProvidersOptions = {},
): RenderResult & { queryClient: ReturnType<typeof createTestQueryClient> } {
  const queryClient = createTestQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        {children}
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}
