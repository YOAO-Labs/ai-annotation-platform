import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { server } from "@/mocks/server";
import schema from "../../../api/openapi.snapshot.json";
import { invitationsApi } from "./invitations";
import { usersApi } from "./users";

type Path = keyof typeof schema.paths;
type Operation = { parameters?: { in: string; name: string; required?: boolean }[] };

async function expectRequest(
  send: () => Promise<unknown>,
  path: Path,
  {
    method = "GET",
    pathname = path,
    query = {},
    body,
  }: {
    method?: "GET" | "POST";
    pathname?: string;
    query?: Record<string, string>;
    body?: unknown;
  },
) {
  const expectedQuery = Object.entries(query).sort();
  const expectedBody = structuredClone(body);
  const requests: Request[] = [];
  server.use(
    http[method === "GET" ? "get" : "post"](`*${pathname}`, ({ request }) => {
      requests.push(request.clone());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  await send();
  expect(requests.map((r) => `${r.method} ${new URL(r.url).pathname}`)).toEqual([
    `${method} ${pathname}`,
  ]);
  const [request] = requests;
  const sentQuery = new URL(request.url).searchParams;
  expect([...sentQuery].sort()).toEqual(expectedQuery);
  const operation = (schema.paths[path] as Record<string, Operation>)[request.method.toLowerCase()];
  expect(operation, `${request.method} ${path}`).toBeDefined();
  const parameters = operation.parameters?.filter((p) => p.in === "query") ?? [];
  const allowed = parameters.map((p) => p.name);
  for (const key of sentQuery.keys()) expect(allowed, `${path}: ${key}`).toContain(key);
  for (const p of parameters.filter((p) => p.required)) {
    expect(sentQuery.has(p.name), `${path}: ${p.name}`).toBe(true);
  }
  if (expectedBody !== undefined) expect(await request.json()).toEqual(expectedBody);
}

const project = "27ecf7b2-6338-41db-9435-601b49ec6295";
const group = "90732d49-b647-45e1-af6a-f673675e493a";
const user = "ac213df5-3df4-4c05-912f-9ff130e1b586";

it("成员和邀请的列表与统计使用相同筛选，只有列表发送分页", async () => {
  const member = {
    role: "employee",
    project_id: project,
    group_id: group,
    status: "inactive",
    search: "A+B & 陈",
  } as const;
  const invite = {
    role: "viewer",
    project_id: project,
    scope: "all",
    status: "pending",
    search: "a+b@example.org",
  } as const;
  const page = { page: 2, page_size: 25 };
  const pageQuery = { page: "2", page_size: "25" };
  await expectRequest(() => usersApi.page({ ...member, ...page }), "/api/v1/users/query", {
    query: { ...member, ...pageQuery },
  });
  await expectRequest(() => usersApi.stats(member), "/api/v1/users/stats", { query: member });
  await expectRequest(
    () => invitationsApi.page({ ...invite, ...page }),
    "/api/v1/invitations/query",
    {
      query: { ...invite, ...pageQuery },
    },
  );
  await expectRequest(() => invitationsApi.stats(invite), "/api/v1/invitations/stats", {
    query: invite,
  });
});

it("批量邀请和分组不能把预览发往写入端点；清除目标组显式发送 null", async () => {
  const items = [
    {
      email: "a+b@example.org",
      role: "employee",
      project_id: project,
      project_member_role: "reviewer",
    },
  ];
  const command = { method: "POST", body: { items } } as const;
  await expectRequest(
    () => usersApi.previewBulkInvite(items),
    "/api/v1/users/bulk-invite/preview",
    command,
  );
  await expectRequest(() => usersApi.bulkInvite(items), "/api/v1/users/bulk-invite", command);
  const assign = { user_ids: [user], group_id: group };
  const clear = { user_ids: [user], group_id: null };
  await expectRequest(
    () => usersApi.previewBulkGroup(assign),
    "/api/v1/users/groups/bulk/preview",
    {
      method: "POST",
      body: assign,
    },
  );
  await expectRequest(() => usersApi.bulkGroup(clear), "/api/v1/users/groups/bulk", {
    method: "POST",
    body: clear,
  });
});

it("角色预览保留目标角色，用户标识编码为单个路径段", async () => {
  await expectRequest(
    () => usersApi.previewRoleChange("u/1", "viewer"),
    "/api/v1/users/{user_id}/role/preview",
    {
      pathname: "/api/v1/users/u%2F1/role/preview",
      query: { role: "viewer" },
    },
  );
});

it("换新链接和发邮件各自只访问一个端点，不能互换或同时触发", async () => {
  const id = "d87e5ea7-75c4-43dc-b73c-d18d8607283d";
  await expectRequest(
    () => invitationsApi.resend(id),
    "/api/v1/invitations/{invitation_id}/resend",
    {
      method: "POST",
      pathname: `/api/v1/invitations/${id}/resend`,
    },
  );
  await expectRequest(
    () => invitationsApi.sendEmail(id),
    "/api/v1/invitations/{invitation_id}/send-email",
    {
      method: "POST",
      pathname: `/api/v1/invitations/${id}/send-email`,
    },
  );
});
