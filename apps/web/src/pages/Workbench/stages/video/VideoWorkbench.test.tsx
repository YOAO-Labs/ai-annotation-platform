import { forwardRef, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { captureOriginalOwner, describeOriginal, type OriginalIdentity } from "@/api/originals";
import { prepareOriginalVideo } from "@/api/originalVideo";
import { useAuthStore } from "@/stores/authStore";
import { bindAuthQueryCache } from "@/stores/authQueryCache";
import type { TaskVideoManifestResponse } from "@/types";
import { VideoWorkbench, type VideoWorkbenchProps } from "./VideoWorkbench";

vi.mock("@/api/originals", async (actual) => ({
  ...(await actual<object>()),
  describeOriginal: vi.fn(),
}));
vi.mock("@/api/originalVideo", () => ({
  prepareOriginalVideo: vi.fn(),
  probeOriginalVideo: vi.fn(),
}));
vi.mock("../../state/useWorkbenchConfig", () => ({
  useWorkbenchConfig: () => ({ config: { video: {}, common: {} } }),
}));
vi.mock("../../stage/annotationVisual", () => ({ resolveAnnotationVisual: () => ({}) }));
vi.mock("../../stage/VideoKonvaStage", () => ({
  VideoKonvaStage: forwardRef(function Stage(
    props: {
      manifest?: TaskVideoManifestResponse;
      sourceStatus?: string;
      isLoading?: boolean;
    },
    _ref,
  ) {
    const [draft, setDraft] = useState(0);
    return (
      <div>
        <span data-testid="source">{props.sourceStatus}</span>
        <span data-testid="metadata">{props.manifest?.task_id}</span>
        <span data-testid="loading">{String(props.isLoading)}</span>
        <button onClick={() => setDraft(draft + 1)}>draft {draft}</button>
      </div>
    );
  }),
}));

afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
});

it("keeps a confirmed stage through same-session token/cache refresh and retires it on task/owner changes", async () => {
  const taskId = "11111111-1111-1111-1111-111111111111";
  const identity: OriginalIdentity = {
    content_id: "44444444-4444-4444-4444-444444444444",
    sha256: "a".repeat(64),
    size_bytes: 3,
    media_type: "video",
    owner_kind: "dataset_asset",
    original_availability: "unchecked",
    verification_state: "verified",
    download_path: "/unused",
  };
  const manifest = {
    task_id: taskId,
    video_url: null,
    metadata: { fps: 30, frame_count: 100 },
    original_identity_path: `/api/v1/files/tasks/${taskId}/original-identity`,
  } as TaskVideoManifestResponse;
  useAuthStore.getState().setAuth("first-token", { id: "alice" } as MeResponse);
  vi.mocked(describeOriginal).mockResolvedValue(identity);
  const release = vi.fn(async () => {});
  vi.mocked(prepareOriginalVideo)
    .mockReset()
    .mockImplementation(async () => ({
      leaseId: "55555555-5555-5555-5555-555555555555",
      mediaPath: "/api/v1/media/original-playback/55555555-5555-5555-5555-555555555555",
      expiresAt: Date.now() + 300_000,
      identity,
      owner: captureOriginalOwner()!,
      release,
      retireOnUnload: vi.fn(),
    }));
  let finishRefresh!: (value: TaskVideoManifestResponse) => void;
  const refreshed = new Promise<TaskVideoManifestResponse>((resolve) => {
    finishRefresh = resolve;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const unbind = bindAuthQueryCache(client);
  function Harness({ currentTaskId }: { currentTaskId: string }) {
    const token = useAuthStore((state) => state.token);
    const query = useQuery({
      queryKey: ["manifest-refresh-boundary", currentTaskId, token],
      queryFn: () => (token === "first-token" ? Promise.resolve(manifest) : refreshed),
    });
    return (
      <VideoWorkbench
        {...({
          taskId: currentTaskId,
          manifest: query.data,
          isLoading: query.isLoading,
        } as VideoWorkbenchProps)}
      />
    );
  }
  const view = (currentTaskId = taskId) => (
    <QueryClientProvider client={client}>
      <Harness currentTaskId={currentTaskId} />
    </QueryClientProvider>
  );
  try {
    const { rerender } = render(view());
    await waitFor(() => expect(screen.getByTestId("source")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "draft 0" }));
    act(() => useAuthStore.getState().setToken("refreshed-token"));
    expect(screen.getByRole("button", { name: "draft 1" })).toBeInTheDocument();
    expect(screen.getByTestId("metadata")).toHaveTextContent(taskId);
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
    expect(release).not.toHaveBeenCalled();
    await act(async () => {
      finishRefresh(manifest);
    });
    expect(screen.getByRole("button", { name: "draft 1" })).toBeInTheDocument();
    expect(prepareOriginalVideo).toHaveBeenCalledOnce();
    rerender(view("22222222-2222-2222-2222-222222222222"));
    expect(release).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "draft 0" })).toBeInTheDocument();
    expect(screen.getByTestId("metadata")).toBeEmptyDOMElement();
    rerender(view());
    await waitFor(() => expect(screen.getByTestId("source")).toHaveTextContent("ready"));
    fireEvent.click(screen.getByRole("button", { name: "draft 0" }));
    act(() => useAuthStore.getState().setAuth("bob-token", { id: "bob" } as MeResponse));
    expect(screen.getByRole("button", { name: "draft 0" })).toBeInTheDocument();
    expect(release).toHaveBeenCalledTimes(2);
  } finally {
    cleanup();
    unbind();
    client.clear();
  }
});
