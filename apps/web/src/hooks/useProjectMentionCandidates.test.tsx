import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mentionCandidates = vi.fn();
const addMember = vi.fn();
const removeMember = vi.fn();
const transfer = vi.fn();

vi.mock("@/api/projects", () => ({
  projectsApi: {
    mentionCandidates: (...args: unknown[]) => mentionCandidates(...args),
    addMember: (...args: unknown[]) => addMember(...args),
    removeMember: (...args: unknown[]) => removeMember(...args),
    transfer: (...args: unknown[]) => transfer(...args),
  },
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ user: { id: "u1" }, token: "test-token" }),
}));

import {
  useAddProjectMember,
  useProjectMentionCandidates,
  useRemoveProjectMember,
  useTransferProject,
} from "./useProjects";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const candidates = [
  { user_id: "u-owner", user_name: "Owner", user_email: "owner@example.com", kind: "owner" },
];

beforeEach(() => {
  mentionCandidates.mockReset().mockResolvedValue(candidates);
  addMember.mockReset().mockResolvedValue({});
  removeMember.mockReset().mockResolvedValue(undefined);
  transfer.mockReset().mockResolvedValue({});
});

describe("project mention candidate cache", () => {
  it("refetches after a member is added", async () => {
    const hook = renderHook(
      () => ({
        candidates: useProjectMentionCandidates("p1"),
        add: useAddProjectMember("p1"),
      }),
      { wrapper },
    );
    await waitFor(() => expect(hook.result.current.candidates.isSuccess).toBe(true));
    await act(async () => {
      await hook.result.current.add.mutateAsync({ user_id: "u2", role: "annotator" });
    });
    await waitFor(() => expect(mentionCandidates).toHaveBeenCalledTimes(2));
  });

  it("refetches after a member is removed and after an owner transfer", async () => {
    const hook = renderHook(
      () => ({
        candidates: useProjectMentionCandidates("p1"),
        remove: useRemoveProjectMember("p1"),
        transfer: useTransferProject("p1"),
      }),
      { wrapper },
    );
    await waitFor(() => expect(hook.result.current.candidates.isSuccess).toBe(true));
    await act(async () => {
      await hook.result.current.remove.mutateAsync("m1");
    });
    await waitFor(() => expect(mentionCandidates).toHaveBeenCalledTimes(2));
    await act(async () => {
      await hook.result.current.transfer.mutateAsync("u-owner");
    });
    await waitFor(() => expect(mentionCandidates).toHaveBeenCalledTimes(3));
  });
});
