import type { ScreenshotScene } from "./_types";

// 设置页（单路由 /settings，左侧导航 + 右侧内容客户端切 section）。
// 登录的 seed admin = super_admin，故「系统设置」tab 可见。
// section 标签文本见 SettingsPage.tsx:38-44：个人资料 / 标注偏好 / API 密钥 / 我的反馈 / 通知偏好 / 系统设置。
// 用 fullPage 截整页（含左导航 + 当前 section 内容）。

/** 切到指定 section（默认 profile，无需点） */
async function gotoSection(page: import("@playwright/test").Page, label: string) {
  await page.waitForLoadState("networkidle");
  const btn = page.getByRole("button", { name: label, exact: true }).first();
  if (await btn.count()) {
    await btn.click();
    await page.waitForTimeout(400);
  }
  await page.waitForLoadState("networkidle");
}

export const SETTINGS_SCENES: ScreenshotScene[] = [
  {
    name: "settings/profile",
    role: "admin",
    route: () => "/settings",
    prepare: async (page) => {
      // profile 为默认 section，无需切换
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(300);
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/settings/profile.png",
  },
  {
    name: "settings/workbench-prefs",
    role: "admin",
    route: () => "/settings",
    prepare: (page) => gotoSection(page, "标注偏好"),
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/settings/workbench-prefs.png",
  },
  {
    name: "settings/notification-prefs",
    role: "admin",
    route: () => "/settings",
    prepare: (page) => gotoSection(page, "通知偏好"),
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/settings/notification-prefs.png",
  },
  {
    name: "settings/system-smtp",
    role: "admin",
    route: () => "/settings",
    prepare: async (page) => {
      await gotoSection(page, "系统设置");
      const mailGroup = page.getByRole("heading", { name: "邮件与访问地址", exact: true });
      if (await mailGroup.count()) {
        // 系统设置按四组渲染，SMTP 在「邮件与访问地址」组；把整张分组卡片滚到视口顶部，
        // 保证发件人、密码与「发送测试邮件」等全部字段入镜（卡片高约 810px，viewport 900）。
        await mailGroup.evaluate((el) => {
          el.closest("div.rounded-lg")?.scrollIntoView({ block: "start" });
        });
        await page.waitForTimeout(300);
      }
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/settings/system-smtp.png",
  },
  {
    name: "settings/my-feedback",
    role: "admin",
    route: () => "/settings",
    prepare: (page) => gotoSection(page, "我的反馈"),
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/settings/my-feedback.png",
  },
];
