import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import type { OriginalIdentity } from "@/api/originals";
import { server } from "@/mocks/server";
import { useAuthStore } from "@/stores/authStore";
import type { ReviewClaimResponse, TaskResponse } from "@/types";
import { createOriginalImageAddress } from "@/utils/originalImageDisplay";
import { useWorkbenchTaskImage } from "./useWorkbenchTaskImage";

vi.mock("@/utils/originalImageDisplay", () => ({ createOriginalImageAddress: vi.fn() }));

const firstTaskId = "11111111-1111-1111-1111-111111111111";
const nextTaskId = "22222222-2222-2222-2222-222222222222";
const contentId = "33333333-3333-3333-3333-333333333333";
const digest = "a".repeat(64);
const releaseImage = vi.fn();
const inspect = vi.fn();
const download = vi.fn();

function task(id = firstTaskId, status: TaskResponse["status"] = "review") {
  return {
    id,
    status,
    file_type: "image",
    image_width: 100,
    image_height: 100,
  } as TaskResponse;
}

function claim(taskId: string, reviewerId = "reviewer", isSelf = true) {
  return HttpResponse.json<ReviewClaimResponse>({
    task_id: taskId,
    reviewer_id: reviewerId,
    reviewer_claimed_at: "2026-05-11T08:00:00Z",
    is_self: isSelf,
  });
}

function holdResponse() {
  let resolve!: (response: Response) => void;
  const response = new Promise<Response>((done) => {
    resolve = done;
  });
  return { response, resolve };
}

function mockClaim(respond: (id: string, token: string | null) => Response | Promise<Response>) {
  const requestClaim = vi.fn(respond);
  server.use(
    http.post("*/api/v1/tasks/:id/review/claim", ({ params, request }) =>
      requestClaim(String(params.id), request.headers.get("Authorization")),
    ),
  );
  return requestClaim;
}

function renderTask(initialTask = task(), review = true) {
  const client = new QueryClient({ defaultOptions: { mutations: { retryDelay: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(useWorkbenchTaskImage, {
    wrapper,
    initialProps: { task: initialTask, requestedTaskId: initialTask.id, review },
  });
}

beforeEach(() => {
  useAuthStore.getState().setAuth("token", { id: "reviewer" } as MeResponse);
  inspect.mockReset();
  download.mockReset();
  releaseImage.mockReset();
  vi.mocked(createOriginalImageAddress).mockResolvedValue({
    url: "blob:owned",
    release: releaseImage,
  });
  vi.stubGlobal(
    "Image",
    class {
      src = "";
      decode = vi.fn().mockResolvedValue(undefined);
    },
  );
  server.use(
    http.get("*/api/v1/files/tasks/:id/original-identity", ({ params, request }) => {
      inspect(params.id, request.headers.get("Authorization"));
      return HttpResponse.json<OriginalIdentity>({
        content_id: contentId,
        sha256: digest,
        size_bytes: 3,
        media_type: "image",
        owner_kind: "task_original",
        original_availability: "unchecked",
        verification_state: "verified",
        download_path: `/api/v1/files/tasks/${params.id}/original?content_id=${contentId}&sha256=${digest}`,
      });
    }),
    http.get("*/api/v1/files/tasks/:id/original", ({ params, request }) => {
      download(params.id, new URL(request.url).search, request.headers.get("Authorization"));
      return new HttpResponse("img");
    }),
  );
});

afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  vi.unstubAllGlobals();
});

it.each(["self", "other", "refused"] as const)(
  "starts identity and bytes only after the initial claim settles (%s)",
  async (outcome) => {
    const pending = holdResponse();
    const requests: string[] = [];
    const requestClaim = mockClaim(async () => {
      const response = await pending.response;
      requests.push("claim finished");
      return response;
    });
    inspect.mockImplementation(() => requests.push("identity"));
    download.mockImplementation(() => requests.push("bytes"));
    const { result } = renderTask();
    await waitFor(() => expect(requestClaim).toHaveBeenCalledWith(firstTaskId, "Bearer token"));
    expect(result.current.image.contentPending).toBe(true);
    expect(inspect).not.toHaveBeenCalled();
    expect(download).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve(
        outcome === "refused"
          ? HttpResponse.json(
              { detail: { reason: "task_review_assigned_to_other" } },
              { status: 409 },
            )
          : outcome === "other"
            ? claim(firstTaskId, "someone-else", false)
            : claim(firstTaskId),
      );
    });

    await waitFor(() => expect(result.current.image.source?.kind).toBe("single"));
    expect(requests).toEqual(["claim finished", "identity", "bytes"]);
    expect(inspect.mock.calls).toEqual([[firstTaskId, "Bearer token"]]);
    expect(download.mock.calls).toEqual([
      [firstTaskId, `?content_id=${contentId}&sha256=${digest}`, "Bearer token"],
    ]);
    await waitFor(() => expect(result.current.reviewClaim.canDecide).toBe(outcome === "self"));
  },
);

it("waits for the automatic busy retry before admitting the original", async () => {
  const retry = holdResponse();
  const requestClaim = mockClaim(() => retry.response).mockReturnValueOnce(
    HttpResponse.json({ detail: { reason: "task_busy" } }, { status: 409 }),
  );
  const { result } = renderTask();
  await waitFor(() => expect(requestClaim).toHaveBeenCalledTimes(2));
  expect(result.current.image.contentPending).toBe(true);
  expect(inspect).not.toHaveBeenCalled();

  await act(async () => retry.resolve(claim(firstTaskId)));
  await waitFor(() => expect(result.current.image.source?.kind).toBe("single"));
  expect(download).toHaveBeenCalledTimes(1);
});

it("keeps the admitted image mounted while retrying a failed claim by hand", async () => {
  const retry = holdResponse();
  const requestClaim = mockClaim(() => retry.response).mockReturnValueOnce(
    HttpResponse.json({ detail: "try again" }, { status: 429 }),
  );
  const { result } = renderTask();
  await waitFor(() => expect(result.current.image.source?.kind).toBe("single"));
  await waitFor(() => expect(result.current.reviewClaim.retry).toBeDefined());
  const source = result.current.image.source;

  act(() => result.current.reviewClaim.retry?.());
  await waitFor(() => expect(requestClaim).toHaveBeenCalledTimes(2));
  expect(result.current.image.source).toEqual(source);
  expect(result.current.reviewClaim.canDecide).toBe(false);

  await act(async () => retry.resolve(claim(firstTaskId)));
  await waitFor(() => expect(result.current.reviewClaim.canDecide).toBe(true));
  expect(result.current.image.source).toEqual(source);
  expect(releaseImage).not.toHaveBeenCalled();
  expect(inspect).toHaveBeenCalledTimes(1);
  expect(download).toHaveBeenCalledTimes(1);
});

it.each(["task", "actor", "session"] as const)(
  "does not unlock the active original with a late claim from a retired %s",
  async (change) => {
    const retired = holdResponse();
    const active = holdResponse();
    const requestClaim = mockClaim(() => active.response).mockReturnValueOnce(retired.response);
    const { result, rerender } = renderTask();
    await waitFor(() => expect(requestClaim).toHaveBeenCalledTimes(1));

    const activeTask = task(change === "task" ? nextTaskId : firstTaskId);
    const activeActor = change === "actor" ? "another-reviewer" : "reviewer";
    const activeToken = change === "task" ? "token" : "new-token";
    if (change === "task") {
      await act(async () => {
        rerender({ task: task(), requestedTaskId: activeTask.id, review: true });
      });
      expect(requestClaim).toHaveBeenCalledTimes(1);
      expect(inspect).not.toHaveBeenCalled();
    }
    await act(async () => {
      if (change !== "task") {
        useAuthStore.getState().setAuth(activeToken, { id: activeActor } as MeResponse);
      }
      rerender({ task: activeTask, requestedTaskId: activeTask.id, review: true });
    });
    await waitFor(() => expect(requestClaim).toHaveBeenCalledTimes(2));
    expect(requestClaim).toHaveBeenLastCalledWith(activeTask.id, `Bearer ${activeToken}`);

    await act(async () => retired.resolve(claim(firstTaskId)));
    expect(result.current.image.contentPending).toBe(true);
    expect(inspect).not.toHaveBeenCalled();
    expect(download).not.toHaveBeenCalled();

    await act(async () => active.resolve(claim(activeTask.id, activeActor)));
    await waitFor(() => expect(result.current.image.source?.kind).toBe("single"));
    expect(inspect.mock.calls).toEqual([[activeTask.id, `Bearer ${activeToken}`]]);
    expect(download).toHaveBeenCalledTimes(1);
  },
);

it.each([
  { status: "review", review: false },
  { status: "completed", review: true },
] as const)(
  "reads outside active review without starting a claim: %j",
  async ({ status, review }) => {
    const requestClaim = mockClaim(() => claim(firstTaskId));
    const { result } = renderTask(task(firstTaskId, status), review);

    await waitFor(() => expect(result.current.image.source?.kind).toBe("single"));
    expect(requestClaim).not.toHaveBeenCalled();
    expect(download).toHaveBeenCalledTimes(1);
  },
);
