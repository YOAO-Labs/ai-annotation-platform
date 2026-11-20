import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as Sentry from "@sentry/react";
import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { initThemeFromStorage } from "./hooks/useTheme";
import { bindAuthQueryCache } from "./stores/authQueryCache";
import { bindAuthStorage } from "./stores/authStore";
import { bindSettingsHistoryGuard } from "./pages/Settings/useUnsavedSettingsGuard";
import "./styles/shadcn.css";

// Sentry：DSN 留空则完全不启用（dev 默认关闭）
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN as string | undefined;
if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: import.meta.env.MODE ?? "development",
    tracesSampleRate: 0.1,
    integrations: [Sentry.browserTracingIntegration()],
  });
}

initThemeFromStorage();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
    },
  },
});
const unbindAuthQueryCache = bindAuthQueryCache(queryClient);
const unbindAuthStorage = bindAuthStorage();
const unbindSettingsHistoryGuard = bindSettingsHistoryGuard();
if (import.meta.hot)
  import.meta.hot.dispose(() => {
    unbindAuthQueryCache();
    unbindAuthStorage();
    unbindSettingsHistoryGuard();
  });

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
