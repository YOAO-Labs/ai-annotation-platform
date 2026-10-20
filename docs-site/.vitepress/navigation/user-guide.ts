import type { DefaultTheme } from "vitepress";

// 侧栏按「任务流程」组织；物理目录继续按稳定产品归属维护。
export const userGuideSidebar: DefaultTheme.SidebarItem[] = [
  {
    text: "开始使用",
    items: [
      { text: "概述", link: "/user-guide/" },
      { text: "平台概念与术语", link: "/user-guide/concepts" },
      { text: "快速开始", link: "/user-guide/getting-started" },
      { text: "使用筛选", link: "/user-guide/reference/filtering" },
    ],
  },
  {
    text: "数据准备",
    collapsed: true,
    items: [
      { text: "数据集总览", link: "/user-guide/datasets/" },
      { text: "导入图像数据集", link: "/user-guide/datasets/import-images" },
      { text: "导入点云 / 多模态", link: "/user-guide/datasets/import-formats" },
      { text: "存储连接器导入", link: "/user-guide/datasets/storage-connections" },
      { text: "点云坐标系约定", link: "/user-guide/datasets/lidar-axis-convention" },
      { text: "导入 / 导出外部预测", link: "/user-guide/datasets/prediction-import-export" },
    ],
  },
  {
    text: "项目与任务",
    collapsed: true,
    items: [
      { text: "项目管理", link: "/user-guide/projects/" },
      { text: "工具维度类别 / 属性", link: "/user-guide/projects/tool-units" },
      { text: "Data Manager", link: "/user-guide/projects/data-manager" },
      { text: "批次与分配", link: "/user-guide/projects/batch" },
      { text: "AI 预标注", link: "/user-guide/projects/ai-preannotate" },
      { text: "在线学习与模型训练", link: "/user-guide/projects/online-learning" },
      { text: "训练模型回流与发布", link: "/user-guide/projects/model-return" },
      { text: "全局编排库", link: "/user-guide/projects/pipeline-library" },
      { text: "ML 后端绑定", link: "/user-guide/projects/ml-backends" },
      { text: "项目模板", link: "/user-guide/projects/project-templates" },
    ],
  },
  {
    text: "标注工作台",
    collapsed: true,
    items: [
      { text: "工作台概览与快捷键", link: "/user-guide/workbench/" },
      { text: "工作台设置", link: "/user-guide/workbench/settings" },
      { text: "评论与问题", link: "/user-guide/workbench/discussion" },
      {
        text: "图片标注",
        collapsed: true,
        items: [
          { text: "Bbox 标注", link: "/user-guide/workbench/bbox" },
          { text: "旋转框标注 (OBB)", link: "/user-guide/workbench/rotated-bbox" },
          { text: "Polygon 标注", link: "/user-guide/workbench/polygon" },
          { text: "折线标注", link: "/user-guide/workbench/polyline" },
          { text: "关键点标注", link: "/user-guide/workbench/keypoint" },
          { text: "Mask 笔刷编辑器", link: "/user-guide/workbench/mask-brush" },
          { text: "SAM 智能工具", link: "/user-guide/workbench/sam-tool" },
        ],
      },
      {
        text: "视频标注",
        collapsed: true,
        items: [
          { text: "视频追踪标注", link: "/user-guide/workbench/video-track" },
          { text: "播放、帧导航与采样", link: "/user-guide/workbench/video-playback" },
          { text: "关键帧传播与 AI", link: "/user-guide/workbench/video-propagate" },
        ],
      },
      {
        text: "点云标注",
        collapsed: true,
        items: [
          { text: "点云视图与上色", link: "/user-guide/workbench/pointcloud-view" },
          { text: "3D 立体框标注", link: "/user-guide/workbench/3d-box" },
          { text: "点云跨模态联动", link: "/user-guide/workbench/pointcloud-projection" },
          { text: "点云跨帧标注", link: "/user-guide/workbench/pointcloud-crossframe" },
        ],
      },
    ],
  },
  {
    text: "AI 辅助",
    collapsed: true,
    items: [
      { text: "AI 能力总览", link: "/user-guide/ai/" },
      { text: "单图／单帧 AI 与二次推理", link: "/user-guide/ai/current-task-inference" },
      { text: "审阅 AI 候选", link: "/user-guide/ai/candidate-review" },
      { text: "交互式智能工具 (SAM)", link: "/user-guide/workbench/sam-tool" },
      { text: "批量与多阶段预标", link: "/user-guide/projects/ai-preannotate" },
      { text: "在线学习与模型训练", link: "/user-guide/projects/online-learning" },
      { text: "训练模型回流与发布", link: "/user-guide/projects/model-return" },
      { text: "视频 AI 追踪", link: "/user-guide/workbench/video-propagate" },
      { text: "失败预测恢复", link: "/user-guide/workflows/failed-prediction-recovery" },
    ],
  },
  {
    text: "审核与交付",
    collapsed: true,
    items: [
      { text: "审核流程", link: "/user-guide/review/" },
      { text: "数据导出格式", link: "/user-guide/reference/export-formats" },
    ],
  },
  {
    text: "平台管理",
    collapsed: true,
    items: [
      { text: "平台管理概览", link: "/user-guide/superadmin/" },
      { text: "用户与权限", link: "/user-guide/superadmin/user-management" },
      { text: "ML Backend 注册", link: "/user-guide/superadmin/ml-backend-registry" },
      { text: "模型市场", link: "/user-guide/superadmin/model-market" },
      { text: "ML Backend 性能基准", link: "/user-guide/superadmin/ml-backend-performance" },
      { text: "失败预测排查", link: "/user-guide/superadmin/failed-predictions" },
      { text: "审计日志", link: "/user-guide/superadmin/audit-logs" },
      { text: "系统监控", link: "/user-guide/superadmin/system-monitoring" },
      { text: "BUG 反馈管理", link: "/user-guide/superadmin/bug-management" },
      { text: "离线分析", link: "/user-guide/superadmin/analytics" },
      { text: "公共模板治理", link: "/user-guide/superadmin/public-templates" },
    ],
  },
  {
    text: "场景方案与支持",
    collapsed: true,
    items: [
      { text: "常见问题", link: "/user-guide/faq" },
      { text: "通知中心", link: "/user-guide/reference/notifications" },
      { text: "账号与偏好设置", link: "/user-guide/reference/settings" },
      { text: "新项目端到端", link: "/user-guide/workflows/new-project-end-to-end" },
      { text: "AI 预标注流水线", link: "/user-guide/workflows/ai-preannotate-pipeline" },
      { text: "失败预测恢复", link: "/user-guide/workflows/failed-prediction-recovery" },
    ],
  },
];
