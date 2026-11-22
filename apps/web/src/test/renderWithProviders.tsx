import { QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement, ReactNode } from "react";

import { createTestQueryClient } from "./queryClient";

export interface RenderWithProvidersOptions {
  initialEntries?: string[];
  children?: ReactNode;
}

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
