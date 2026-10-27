import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectorySyncSchedule } from "./DirectorySyncSchedule";
const api = vi.hoisted(() => ({
  syncSchedule: vi.fn(),
  configureSync: vi.fn(),
  continueSync: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionGuard: () => () => () => true }));
beforeEach(() => {
  api.syncSchedule.mockResolvedValue(null);
  api.configureSync.mockResolvedValue({});
});
function mount() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DirectorySyncSchedule sourceId="source" owner="session" datasetId="dataset" />
    </QueryClientProvider>,
  );
}
it("defaults to human confirmation and submits explicit automation with timezone", async () => {
  mount();
  const button = await screen.findByRole("button", { name: "启用周期扫描" });
  await waitFor(() => expect(button).toBeEnabled());
  expect(screen.getByRole("combobox", { name: "新增接纳策略" })).toHaveValue("manual");
  fireEvent.change(screen.getByRole("combobox", { name: "新增接纳策略" }), {
    target: { value: "pure_additions" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "周期计划时区" }), {
    target: { value: "Asia/Shanghai" },
  });
  fireEvent.click(button);
  await waitFor(() =>
    expect(api.configureSync).toHaveBeenCalledWith("source", {
      expected_revision: 0,
      enabled: true,
      interval_minutes: 60,
      new_file_policy: "pure_additions",
      timezone: "Asia/Shanghai",
    }),
  );
});
it("pause preserves saved policy and displays the configured timezone", async () => {
  api.syncSchedule.mockResolvedValue({
    revision: 4,
    enabled: true,
    state: "registering",
    interval_minutes: 15,
    new_file_policy: "pure_additions",
    timezone: "Asia/Shanghai",
    next_run_at: "2026-10-23T00:00:00Z",
  });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "暂停周期扫描" }));
  await waitFor(() =>
    expect(api.configureSync).toHaveBeenCalledWith("source", {
      expected_revision: 4,
      enabled: false,
      interval_minutes: 15,
      new_file_policy: "pure_additions",
      timezone: "Asia/Shanghai",
    }),
  );
  expect(screen.getByRole("status")).toHaveTextContent("时区 Asia/Shanghai");
});

it("falls back to explicit UTC when the browser cannot format a saved timezone", async () => {
  api.syncSchedule.mockResolvedValue({
    revision: 4,
    enabled: true,
    state: "idle",
    interval_minutes: 15,
    new_file_policy: "manual",
    timezone: "unsupported",
    next_run_at: "2026-10-23T00:00:00Z",
  });
  mount();
  expect(await screen.findByRole("status")).toHaveTextContent("UTC，本机不支持已选时区");
});
