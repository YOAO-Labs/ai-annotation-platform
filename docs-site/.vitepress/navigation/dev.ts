import type { DefaultTheme } from "vitepress";

export function createDevSidebar(
  adrSidebarItems: DefaultTheme.SidebarItem[],
): DefaultTheme.SidebarItem[] {
  return [
    {
      text: "开始开发",
      items: [
        { text: "概览", link: "/dev/" },
        { text: "本地开发", link: "/dev/tutorials/local-dev" },
        { text: "第一个贡献", link: "/dev/tutorials/first-contribution" },
        { text: "ML Backend 接入教程", link: "/dev/ml-backend/starter" },
      ],
    },
    {
      text: "操作指南",
      collapsed: true,
      items: [
        { text: "编写文档", link: "/dev/how-to/write-documentation" },
        { text: "新增 API 端点", link: "/dev/how-to/add-api-endpoint" },
        { text: "新增前端页面", link: "/dev/how-to/add-page" },
        { text: "Alembic 迁移", link: "/dev/how-to/add-migration" },
        { text: "调试 Celery", link: "/dev/how-to/debug-celery" },
        { text: "调试 WebSocket", link: "/dev/how-to/debug-websocket" },
        { text: "更新截图", link: "/dev/how-to/update-screenshots" },
        { text: "更新 Excalidraw 图表", link: "/dev/how-to/update-excalidraw-diagrams" },
        {
          text: "迁移内联样式到 CSS Modules",
          link: "/dev/how-to/migrate-inline-style-to-css-modules",
        },
        { text: "视频工作台性能回归", link: "/dev/how-to/video-workbench-performance-regression" },
      ],
    },
    {
      text: "系统架构",
      collapsed: true,
      items: [
        {
          text: "总览",
          collapsed: true,
          items: [
            { text: "架构地图", link: "/dev/concepts/" },
            { text: "系统全景", link: "/dev/concepts/overview" },
            {
              text: "仓库地图（模块归属与调用链）",
              link: "/dev/concepts/repository-map",
            },
          ],
        },
        {
          text: "业务域模型",
          collapsed: true,
          items: [
            { text: "项目模块", link: "/dev/concepts/project-module" },
            { text: "批次模块", link: "/dev/concepts/batch-module" },
            { text: "任务模块", link: "/dev/concepts/task-module" },
            { text: "标注模块", link: "/dev/concepts/annotation-module" },
            { text: "视频标注工作台", link: "/dev/concepts/video-annotation-workbench" },
            { text: "Scene 与帧索引", link: "/dev/concepts/scene-and-frame-index" },
            { text: "审核模块", link: "/dev/concepts/review-module" },
          ],
        },
        {
          text: "工作流与协作机制",
          collapsed: true,
          items: [
            { text: "状态机总览", link: "/dev/concepts/state-machines" },
            { text: "Scheduler 与派题", link: "/dev/concepts/scheduler-and-task-dispatch" },
            { text: "Task Lock", link: "/dev/concepts/task-locking" },
            { text: "可见性与权限", link: "/dev/concepts/visibility-and-permissions" },
            { text: "计数与派生字段", link: "/dev/concepts/counters-and-derived-fields" },
            { text: "审计与通知", link: "/dev/concepts/audit-and-notifications" },
            { text: "反馈收敛与双写对账", link: "/dev/concepts/feedback-convergence" },
          ],
        },
        {
          text: "端到端业务流程",
          collapsed: true,
          items: [
            { text: "批次生命周期（端到端）", link: "/dev/concepts/batch-lifecycle-end-to-end" },
            { text: "AI 预标注接管", link: "/dev/concepts/ai-preannotate-handoff" },
            { text: "数据流", link: "/dev/concepts/data-flow" },
            { text: "存储连接器", link: "/dev/concepts/storage-connections" },
            { text: "系统设置运行时覆盖", link: "/dev/concepts/system-settings" },
          ],
        },
        {
          text: "AI 与推理子系统",
          collapsed: true,
          items: [
            { text: "预标注流水线", link: "/dev/concepts/prediction-pipeline" },
            { text: "视频 AI 追踪", link: "/dev/concepts/video-ai-tracking" },
            { text: "AI 模型集成", link: "/dev/concepts/ai-models" },
          ],
        },
        {
          text: "平台实现架构",
          collapsed: true,
          items: [
            { text: "后端分层", link: "/dev/concepts/backend-layers" },
            { text: "前端分层", link: "/dev/concepts/frontend-layers" },
            { text: "工作台 Shell 架构", link: "/dev/concepts/workbench-shell" },
            { text: "API Schema 边界", link: "/dev/concepts/api-schema-boundary" },
            { text: "后端基础设施（容器）", link: "/dev/concepts/backend-infrastructure" },
            { text: "部署拓扑", link: "/dev/concepts/deployment-topology" },
            {
              text: "运行环境形态（dev/staging/prod）",
              link: "/dev/concepts/runtime-environments",
            },
            { text: "性能 HUD", link: "/dev/concepts/perfhud" },
            { text: "超大图金字塔派生资产", link: "/dev/concepts/image-pyramid-assets" },
            { text: "图片工作台栅格资源协调", link: "/dev/concepts/raster-resource-coordination" },
          ],
        },
      ],
    },
    {
      text: "SDK 与 CLI",
      collapsed: true,
      items: [
        { text: "快速上手", link: "/dev/sdk/quickstart" },
        { text: "Python SDK 参考", link: "/dev/sdk/python-client" },
        { text: "CLI 参考", link: "/dev/sdk/cli" },
        { text: "TUI 监控面板", link: "/dev/sdk/tui" },
        { text: "Cookbook", link: "/dev/sdk/cookbook" },
      ],
    },
    {
      text: "协议与规范",
      collapsed: true,
      items: [
        { text: "ML Backend 协议", link: "/dev/reference/ml-backend-protocol" },
        { text: "YOLO 导入适配", link: "/dev/reference/yolo-import" },
        { text: "WebSocket 协议", link: "/dev/reference/ws-protocol" },
        { text: "视频帧服务", link: "/dev/reference/video-frame-service" },
        { text: "点云联合标注数据模型", link: "/dev/reference/point-cloud-data-model" },
        { text: "点云导出格式", link: "/dev/reference/lidar-export-formats" },
        { text: "Data Manager 查询与聚合", link: "/dev/reference/data-manager-query" },
        { text: "设计系统", link: "/dev/reference/design-system" },
        { text: "代码规范", link: "/dev/reference/conventions" },
        { text: "图标约定", link: "/dev/reference/icon-conventions" },
        { text: "环境变量", link: "/dev/reference/env-vars" },
        { text: "存储桶布局", link: "/dev/reference/storage-buckets" },
        { text: "连接器安全", link: "/dev/reference/connector-security" },
        { text: "内部 API 端点", link: "/dev/reference/internal-api-endpoints" },
        { text: "生成物归属", link: "/dev/reference/generated-artifacts" },
        { text: "高清营销资产", link: "/dev/reference/marketing-asset-catalog" },
      ],
    },
    {
      text: "测试与发布",
      collapsed: true,
      items: [
        { text: "测试指南", link: "/dev/testing" },
        { text: "发布流程", link: "/dev/release" },
      ],
    },
    {
      text: "故障排查",
      collapsed: true,
      items: [
        { text: "总览与速查表", link: "/dev/troubleshooting/" },
        {
          text: "Docker rebuild vs restart",
          link: "/dev/troubleshooting/docker-rebuild-vs-restart",
        },
        { text: "容器网络与 loopback", link: "/dev/troubleshooting/container-networking" },
        { text: "Prediction Schema 适配器", link: "/dev/troubleshooting/schema-adapter-pitfalls" },
        { text: "Dev 数据保护", link: "/dev/troubleshooting/dev-data-preservation" },
        { text: "React useState TDZ", link: "/dev/troubleshooting/react-tdz-trap" },
        { text: "环境变量与 config 路径", link: "/dev/troubleshooting/env-and-config-paths" },
        { text: "CI 服务依赖踩坑", link: "/dev/troubleshooting/ci-flaky-services" },
      ],
    },
    {
      text: "架构决策",
      collapsed: true,
      items: adrSidebarItems,
    },
  ];
}
