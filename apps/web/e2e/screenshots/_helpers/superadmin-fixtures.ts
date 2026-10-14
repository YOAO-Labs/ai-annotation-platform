import type { Page } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";

export const API_PREFIX = "/api/v1";
export const GROUP_FIXTURE = {
  name: "截图示例 · 项目协作组",
  description: "文档截图示例组；成员为截图种子中的标注员与质检员。",
};
const TEMPLATE_FIXTURES = [
  {
    name: "截图示例 · 项目模板",
    description: "从截图种子项目导出的项目配置示例。",
    scope: "private",
  },
  {
    name: "截图示例 · 公共模板",
    description: "由超级管理员发布的公共项目配置示例。",
    scope: "public",
  },
] as const;

type ApiMethod = "GET" | "POST" | "PATCH" | "DELETE";

interface ScreenshotUserRow {
  id: string;
  email: string;
  role: string;
  group_id: string | null;
  is_active: boolean;
}

interface ScreenshotGroupRow {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
}

interface ScreenshotTemplateRow {
  id: string;
  name: string;
  description: string | null;
  scope: "private" | "organization" | "public";
  created_by: string;
  source_project_id: string | null;
  usage_count: number;
}

async function screenshotApi<T>(
  page: Page,
  method: ApiMethod,
  path: string,
  token: string,
  data?: unknown,
): Promise<T> {
  const response = await page.request.fetch(`${API_PREFIX}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, Connection: "close" },
    ...(data === undefined ? {} : { data }),
  });
  if (!response.ok()) {
    throw new Error(
      `[superadmin screenshot] ${method} ${path}: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  if (response.status() === 204) return undefined as T;
  return (await response.json()) as T;
}

async function screenshotToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => localStorage.getItem("token"));
  if (!token) throw new Error("[superadmin screenshot] 缺少截图管理员令牌");
  return token;
}

/** The seed endpoint itself refuses non-test databases and validates the screenshots profile. */
async function verifyScreenshotDatabase(page: Page, catalog: ScreenshotSeedCatalog): Promise<void> {
  const response = await page.request.get(
    `${API_PREFIX}/__test/seed/catalog?profile=screenshots&backend_requirements=none`,
  );
  if (!response.ok()) {
    throw new Error(
      `[superadmin screenshot] screenshot database verification failed: HTTP ${response.status()} ${await response.text()}`,
    );
  }
  const live = (await response.json()) as ScreenshotSeedCatalog;
  if (
    live.seed_revision !== catalog.seed_revision ||
    live.users.admin.id !== catalog.users.admin.id ||
    live.users.annotator.id !== catalog.users.annotator.id ||
    live.users.reviewer.id !== catalog.users.reviewer.id ||
    live.projects.image_demo.id !== catalog.projects.image_demo.id
  ) {
    throw new Error("[superadmin screenshot] screenshot seed catalog changed during capture");
  }
}

async function listScreenshotUsers(page: Page, token: string): Promise<ScreenshotUserRow[]> {
  return screenshotApi<ScreenshotUserRow[]>(page, "GET", "/users?status=all", token);
}

export async function prepareScreenshotGroup(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ group: ScreenshotGroupRow; cleanup: () => Promise<void> }> {
  const token = await screenshotToken(page);
  await verifyScreenshotDatabase(page, catalog);
  const users = await listScreenshotUsers(page, token);
  const members = [catalog.users.annotator, catalog.users.reviewer].map((seeded) => {
    const user = users.find((candidate) => candidate.id === seeded.id);
    if (!user || !user.is_active || user.role !== seeded.role) {
      throw new Error(`[superadmin screenshot] seeded member ${seeded.email} is not ready`);
    }
    return user;
  });
  const group = await screenshotApi<ScreenshotGroupRow>(
    page,
    "POST",
    "/groups",
    token,
    GROUP_FIXTURE,
  );
  const cleanup = async () => {
    const currentUsers = await listScreenshotUsers(page, token);
    if (
      currentUsers.some(
        (user) => user.group_id === group.id && !members.some((member) => member.id === user.id),
      )
    ) {
      throw new Error("[superadmin screenshot] fixture group contains an unexpected member");
    }
    for (const original of members) {
      const current = currentUsers.find((user) => user.id === original.id);
      if (
        !current ||
        current.role !== original.role ||
        (current.group_id !== group.id && current.group_id !== original.group_id)
      ) {
        throw new Error("[superadmin screenshot] seeded member changed outside this capture");
      }
      if (current.group_id === group.id) {
        await screenshotApi<ScreenshotUserRow>(
          page,
          "PATCH",
          `/users/${original.id}/group`,
          token,
          { group_id: original.group_id },
        );
      }
    }
    await screenshotApi<void>(page, "DELETE", `/groups/${group.id}`, token);
  };
  try {
    for (const member of members) {
      const assigned = await screenshotApi<ScreenshotUserRow>(
        page,
        "PATCH",
        `/users/${member.id}/group`,
        token,
        { group_id: group.id },
      );
      if (assigned.group_id !== group.id || assigned.role !== member.role) {
        throw new Error("[superadmin screenshot] data-group API changed a seeded member role");
      }
    }
    const savedGroup = (
      await screenshotApi<ScreenshotGroupRow[]>(page, "GET", "/groups", token)
    ).find((candidate) => candidate.id === group.id);
    if (!savedGroup || savedGroup.member_count !== members.length) {
      throw new Error(
        "[superadmin screenshot] data-group member count did not reflect real assignments",
      );
    }
    return { group: savedGroup, cleanup };
  } catch (error) {
    await cleanup();
    throw error;
  }
}

export async function prepareScreenshotTemplates(
  page: Page,
  catalog: ScreenshotSeedCatalog,
): Promise<{ templates: ScreenshotTemplateRow[]; cleanup: () => Promise<void> }> {
  const token = await screenshotToken(page);
  await verifyScreenshotDatabase(page, catalog);
  const project = await screenshotApi<{
    id: string;
    name: string;
    type_label: string;
    type_key: string;
  }>(page, "GET", `/projects/${catalog.projects.image_demo.id}`, token);
  if (
    project.id !== catalog.projects.image_demo.id ||
    !project.name ||
    !project.type_label ||
    !project.type_key
  ) {
    throw new Error("[superadmin screenshot] catalog project is missing template source fields");
  }
  const created: ScreenshotTemplateRow[] = [];
  const cleanup = async () => {
    const templates = await screenshotApi<ScreenshotTemplateRow[]>(
      page,
      "GET",
      "/project-templates",
      token,
    );
    for (const original of created) {
      const current = templates.find((template) => template.id === original.id);
      if (
        !current ||
        current.created_by !== catalog.users.admin.id ||
        current.source_project_id !== project.id ||
        current.scope !== original.scope ||
        current.usage_count !== 0
      ) {
        throw new Error("[superadmin screenshot] fixture template changed outside this capture");
      }
      await screenshotApi<void>(page, "DELETE", `/project-templates/${current.id}`, token);
    }
  };
  try {
    for (const fixture of TEMPLATE_FIXTURES) {
      const template = await screenshotApi<ScreenshotTemplateRow>(
        page,
        "POST",
        "/project-templates",
        token,
        {
          name: fixture.name,
          description: fixture.description,
          type_label: project.type_label,
          type_key: project.type_key,
          source_project_id: project.id,
          scope: fixture.scope,
        },
      );
      created.push(template);
      if (
        template.created_by !== catalog.users.admin.id ||
        template.source_project_id !== project.id ||
        template.scope !== fixture.scope ||
        template.usage_count !== 0
      ) {
        throw new Error(
          "[superadmin screenshot] project-template API returned unexpected fixture data",
        );
      }
    }
  } catch (error) {
    await cleanup();
    throw error;
  }
  return { templates: created, cleanup };
}
