import { afterEach, expect, it, vi } from "vitest";
import { resetAuthUser, seedAuthUser, createTestUser } from "@/test/auth";
import { auditApi } from "./audit";
import { dashboardApi } from "./dashboard";
import { invitationsApi } from "./invitations";
import { usersApi } from "./users";

afterEach(() => {
  vi.unstubAllGlobals();
  resetAuthUser();
});

it.each([
  ["成员", () => usersApi.exportUsers("csv", { search: "Alice" })],
  ["邀请", () => invitationsApi.exportInvitations({ scope: "me" })],
  ["人员绩效", () => dashboardApi.exportPeople({ q: "Alice" })],
  ["审计", () => auditApi.export({ detail_key: "role", detail_value: "" })],
] as const)("%s：收到响应头后换账号，不发布上一账号的 CSV", async (_name, download) => {
  seedAuthUser(createTestUser({ id: "first" }), "first-token");
  const reading = vi.fn();
  let body!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>(
    {
      start(controller) {
        body = controller;
      },
      pull: reading,
    },
    { highWaterMark: 0 },
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream)));
  const publish = vi.fn(() => {
    throw new Error("published the previous account's download");
  });
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = publish;
    },
  );

  const rejected = expect(download()).rejects.toThrow("当前登录状态已改变");
  await vi.waitFor(() => expect(reading).toHaveBeenCalled());
  seedAuthUser(createTestUser({ id: "replacement" }), "replacement-token");
  body.enqueue(new TextEncoder().encode("private export"));
  body.close();
  await rejected;
  expect(publish).not.toHaveBeenCalled();
});
