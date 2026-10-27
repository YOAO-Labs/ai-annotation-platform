import type { DefaultTheme } from "vitepress";

// Organize the sidebar by user workflow, with online learning as a dedicated chapter.
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
    text: "数据集与存储",
    collapsed: false,
    items: [
      { text: "数据集与存储总览", link: "/user-guide/datasets/" },
      {
        text: "数据集与来源管理",
        collapsed: false,
        items: [
          { text: "导入图像与托管数据", link: "/user-guide/datasets/import-images" },
          { text: "数据来源与增量同步", link: "/user-guide/datasets/sources" },
          { text: "数据集工作台与视图", link: "/user-guide/datasets/workbench" },
          { text: "图片质量检测与复核", link: "/user-guide/datasets/quality" },
          { text: "导入点云 / 多模态", link: "/user-guide/datasets/import-formats" },
          { text: "点云坐标系约定", link: "/user-guide/datasets/lidar-axis-convention" },
          { text: "导入 / 导出外部预测", link: "/user-guide/datasets/prediction-import-export" },
        ],
      },
      {
        text: "存储管理与运维",
        collapsed: false,
        items: [
          { text: "存储位置与连接器", link: "/user-guide/datasets/storage-connections" },
          { text: "跨网络站点与 Agent", link: "/user-guide/datasets/site-agents" },
          { text: "九用途映射与受控切换", link: "/user-guide/datasets/purpose-bindings" },
          {
            text: "原件访问、归档与保留回收",
            link: "/user-guide/datasets/lifecycle-and-retention",
          },
          { text: "对象存储迁移准备", link: "/user-guide/datasets/storage-migration" },
          { text: "存储诊断与容量账目", link: "/user-guide/datasets/storage-operations" },
        ],
      },
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
      { text: "视频 AI 追踪", link: "/user-guide/workbench/video-propagate" },
      { text: "失败预测恢复", link: "/user-guide/workflows/failed-prediction-recovery" },
    ],
  },
  {
    text: "在线学习",
    collapsed: false,
    items: [
      { text: "在线学习总览", link: "/user-guide/online-learning/" },
      { text: "完成第一轮与第二轮", link: "/user-guide/online-learning/quick-start" },
      { text: "训练数据版本", link: "/user-guide/online-learning/data-versions" },
      { text: "创建与管理实验", link: "/user-guide/online-learning/experiments" },
      { text: "训练配置与复用", link: "/user-guide/online-learning/configuration" },
      { text: "Ultralytics 训练容器", link: "/user-guide/online-learning/ultralytics" },
      { text: "LibreYOLO 训练容器", link: "/user-guide/online-learning/libreyolo" },
      { text: "训练监控与指标", link: "/user-guide/online-learning/metrics" },
      { text: "实验比较与参数搜索", link: "/user-guide/online-learning/comparison" },
      { text: "模型回流与默认预标", link: "/user-guide/online-learning/models" },
      { text: "逐样本评估", link: "/user-guide/online-learning/sample-evaluation" },
      { text: "自动训练策略", link: "/user-guide/online-learning/automation" },
      { text: "主动学习选样", link: "/user-guide/online-learning/active-learning" },
      { text: "常见问题与能力边界", link: "/user-guide/online-learning/troubleshooting" },
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
