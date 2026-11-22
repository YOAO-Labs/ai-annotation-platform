import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { ApiKey } from "@/api/apiKeys";

const mocks = vi.hoisted(() => ({
  revokeMutate: vi.fn(),
  rotateMutate: vi.fn(),
  key: {
    id: "key-1",
    name: "CI 密钥",
    key_prefix: "ak_live",
    scopes: ["*"],
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    created_at: "2026-09-01T00:00:00Z",
  } satisfies ApiKey,
}));

vi.mock("@/hooks/useApiKeys", () => ({
  useApiKeys: () => ({ data: [mocks.key], isLoading: false }),
  useCreateApiKey: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    reset: vi.fn(),
    isPending: false,
  }),
  useUpdateApiKey: () => ({ mutate: vi.fn(), isPending: false }),
  useRotateApiKey: () => ({ mutate: mocks.rotateMutate, isPending: false }),
  useRevokeApiKey: () => ({ mutate: mocks.revokeMutate, isPending: false }),
}));

import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import { ApiKeysPanel } from "./ApiKeysPanel";

function renderPanel() {
  return render(
    <>
      <ApiKeysPanel active />
      <DecisionDialogHost />
    </>,
  );
}

function clickRevoke() {
  act(() => {
    fireEvent.click(screen.getByTitle("吊销密钥"));
  });
}

async function flushTeardown() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

afterEach(() => {
  act(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });
});

describe("ApiKeysPanel 吊销密钥确认", () => {
  it("点吊销先弹确认对话框,取消不执行,确认后才吊销", async () => {
    renderPanel();
    expect(screen.queryByRole("alertdialog")).toBeNull();

    clickRevoke();

    const dialog = await screen.findByRole("alertdialog");
    expect(screen.getByText("吊销 API 密钥")).toBeTruthy();
    expect(dialog.textContent).toContain("CI 密钥");
    expect(dialog.textContent).toContain("不可恢复");
    expect(screen.getByRole("button", { name: "吊销" })).toBeTruthy();
    expect(mocks.revokeMutate).not.toHaveBeenCalled();

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "取消" }));
    });
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await flushTeardown();
    expect(mocks.revokeMutate).not.toHaveBeenCalled();

    clickRevoke();
    await screen.findByRole("alertdialog");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "吊销" }));
    });
    await waitFor(() => expect(mocks.revokeMutate).toHaveBeenCalled());
    expect(mocks.revokeMutate.mock.calls[0]?.[0]).toBe("key-1");
    await flushTeardown();
  });
});
