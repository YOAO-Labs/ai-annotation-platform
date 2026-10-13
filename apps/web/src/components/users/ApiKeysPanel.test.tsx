/**
 * ApiKeysPanel 吊销密钥的决策对话框迁移验证(docs/plans/archive/1789527942 Phase 1 / Group B 验收):
 * 代表性破坏性流程 —— 断言对话框文案(动作+对象、后果、动词确认键),以及
 * 「确认后才执行」:点吊销只弹对话框不调 mutation,取消不执行,确认后才吊销。
 */
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

/** 服务入队即 store 更新,包一层 act 避免测试告警。 */
function clickRevoke() {
  act(() => {
    fireEvent.click(screen.getByTitle("吊销密钥"));
  });
}

/**
 * 触发关闭后,把 Radix Presence 的 rAF 卸载帧(jsdom 约 16ms)放进 act 里冲掉,
 * 让「退场完毕出队」的 store 更新落在 act 内,避免测试告警。
 */
async function flushTeardown() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

// vitest 的 afterEach 按 LIFO 在 RTL cleanup 前跑,此刻 Host 仍挂载,清空队列需包 act。
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
    // 动作+对象为标题,后果进描述,确认键用动词
    expect(screen.getByText("吊销 API 密钥")).toBeTruthy();
    expect(dialog.textContent).toContain("CI 密钥");
    expect(dialog.textContent).toContain("不可恢复");
    expect(screen.getByRole("button", { name: "吊销" })).toBeTruthy();
    // 仅弹窗,尚未执行
    expect(mocks.revokeMutate).not.toHaveBeenCalled();

    // 取消 → 不执行
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "取消" }));
    });
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await flushTeardown();
    expect(mocks.revokeMutate).not.toHaveBeenCalled();

    // 确认 → 以密钥 id 执行吊销(mutate 第二参是回调对象,只断言首参)
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
