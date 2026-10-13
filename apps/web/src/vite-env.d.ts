/// <reference types="vite/client" />
/// <reference types="@webgpu/types" />

/** 版本更新提醒:当前版本 changelog 段落,由 vite-plugins/release-notes.ts 构建期注入。 */
declare module "virtual:release-notes" {
  export const sectionMarkdown: string;
}
