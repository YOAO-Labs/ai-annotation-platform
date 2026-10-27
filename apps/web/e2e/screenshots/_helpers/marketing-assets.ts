export interface MarketingAssetDuration {
  minSeconds: number;
  targetSeconds: number;
  maxSeconds: number;
}

export interface MarketingAssetSpec {
  assetId: string;
  title: string;
  theme: string;
  objective: string;
  duration: MarketingAssetDuration;
  shots: string[];
  editingNotes: string[];
}

function defineAsset(spec: MarketingAssetSpec): MarketingAssetSpec {
  return spec;
}

const assetSpecs = [
  defineAsset({
    assetId: "detection-online-training",
    title: "目标检测在线训练完整流程",
    theme: "冻结数据、真实训练、模型回流与预标注迭代",
    objective:
      "在独立演示环境使用真实检测数据和执行器，展示数据版本、配置实验、训练与验证指标、注册模型、加载试用、默认预标和人工修正回流。",
    duration: { minSeconds: 90, targetSeconds: 240, maxSeconds: 900 },
    shots: [
      "核对已审核图片，创建冻结数据版本并继承固定训练／验证划分。",
      "选择检测任务、YOLO11n 和预训练初始化，配置轮次与训练集评估后开始真实训练。",
      "查看逐轮训练表现、训练损失、验证表现、验证损失和训练产物。",
      "将成功执行添加为项目候选，绑定类别，加载模型并查看固定验证样本。",
      "设为检测默认预标，在工作台使用回流模型生成候选并人工处理。",
      "准备修正后的下一版数据，用项目候选初始化下一轮实验。",
    ],
    editingNotes: [
      "全部训练和推理均为真实执行，不拦截响应或注入指标。",
      "仅在专用测试数据库、Redis、存储桶和受管演示容器中运行。",
      "COCO8 仅用于本次私有开发演示，不自动发布到公共文档素材。",
      "这是流程演示，不代表业务精度提升或目标部署 GPU 生命周期验收。",
    ],
  }),
  defineAsset({
    assetId: "project-onboarding",
    title: "登录、创建项目并进入工作台",
    theme: "首次创建可标注图片项目的完整入门流程",
    objective:
      "展示真实账号登录，创建图片矩形框项目、配置 car 类别、关联已有道路数据集和团队成员，再从创建成功页进入同一新项目的工作台，确认图像与工具栏已加载。",
    duration: { minSeconds: 20, targetSeconds: 30, maxSeconds: 65 },
    shots: [
      "从已加载的登录页开始，输入隔离演示账号与掩码密码，通过正式登录 API 进入项目总览。",
      "打开新建项目向导，填写道路车辆标注项目名，保留图片与矩形框工具配置。",
      "新增 car 类别，经过属性与可选 AI 接入步骤创建项目。",
      "关联平台已有道路数据集，选择现有标注员与审核员。",
      "核对创建成功摘要和新项目编号，明确创建项目不会自动分派任务。",
      "点击打开工作台，展示同一新项目已加载的道路图像、任务面板与矩形框工具。",
    ],
    editingNotes: [
      "登录、项目创建和工作台进入共同构成本素材的单一入门主题，登录必须保留在母版动作窗口内。",
      "登录、项目创建、数据集关联、成员添加与任务读取均使用正式 API，不注入登录令牌或伪造成功响应。",
      "复用已有数据与账号，不表示现场上传、开放注册或已经分派批次；不加入画框、提交或 AI 推理。",
      "录制结束后通过正式级联删除 API 精确清理本次项目，保留原有数据集和账号。",
    ],
  }),
  defineAsset({
    assetId: "workspace-layout-basics",
    title: "工作台布局调整",
    theme: "调整面板时保留标注选择与讨论草稿",
    objective: "展示调整宽度、合并标签、隐藏找回与专注画布，不中断当前编辑上下文。",
    duration: { minSeconds: 20, targetSeconds: 30, maxSeconds: 60 },
    shots: [
      "在标准布局中保持一个已保存标注选中，并留下未发送的讨论。",
      "拖动分隔条，合并讨论和标注详情并切换标签。",
      "隐藏活动标签，再从布局菜单恢复原面板和草稿。",
      "专注画布后恢复，保持同一任务、标注和讨论内容。",
    ],
    editingNotes: ["这是页面内布局教学，不表示未发送草稿会跨刷新持久保存。"],
  }),
  defineAsset({
    assetId: "workspace-layout-persistence",
    title: "工作台布局偏好持久化",
    theme: "布局保存后跨任务、刷新与紧凑视口恢复",
    objective:
      "展示真实账号偏好 PATCH/GET 保存 Dockview 布局，在图像与视频任务间切换后回到原 context，刷新并进入紧凑视口再恢复桌面布局。",
    duration: { minSeconds: 18, targetSeconds: 28, maxSeconds: 55 },
    shots: [
      "在图像工作台把面板切换为专注画布布局并保留完整画布。",
      "切换到视频任务，保存独立的视频追踪布局，证明 image/video context 分桶。",
      "返回图像任务并刷新，核对之前保存的桌面布局仍被真实 GET 恢复。",
      "进入紧凑视口再回到桌面宽度，保持已保存的 Dockview 树与面板可见。",
    ],
    editingNotes: [
      "该素材验证真实 preferences PATCH/GET 与 context 分桶；紧凑视口只临时重排，不写回桌面布局。",
      "保留跨任务、刷新和视口变化后的实际恢复结果，不把未发送讨论草稿写成刷新后仍存。",
    ],
  }),
  defineAsset({
    assetId: "workspace-named-preset",
    title: "命名布局预设",
    theme: "把当前面板排列另存为命名预设并按账号恢复",
    objective:
      "展示在一个已保存并选中的对象上做出自定义排列、另存为「边界精修」预设、切到标准布局后套回、撤销这次应用，并在刷新后确认预设仍随账号保存。",
    duration: { minSeconds: 18, targetSeconds: 26, maxSeconds: 38 },
    shots: [
      "从一个已保存并选中的对象与自定义面板排列开始（任务队列并入类别面板标签，讨论面板隐藏）。",
      "在布局快捷菜单把当前排列另存为「边界精修」预设，展示真实的账号写入结果。",
      "切换到标准标注布局后再套用「边界精修」，排列恢复且选中对象仍在。",
      "撤销这次应用回到上一排列，刷新后预设仍随账号保存并保持同一任务。",
    ],
    editingNotes: [
      "保存与套用都来自真实账号偏好 PATCH/GET，不使用界面沙箱，也不把预设写成跨设备同步。",
      "撤销只回退这一次应用；保留同一任务与同一选中对象的真实状态，不补拍未发送草稿。",
      "录制在隔离账号上完成，结束后恢复该账号的原始偏好。",
    ],
  }),
  defineAsset({
    assetId: "video-comment-badges",
    title: "视频对象评论角标",
    theme: "在当前帧对象上展示评论总数并打开对应讨论",
    objective:
      "展示带评论的轨迹与单帧标注各自的评论角标，在两个已知帧之间逐帧移动后点击可见对象的角标，打开的讨论只包含该对象的评论。",
    duration: { minSeconds: 14, targetSeconds: 22, maxSeconds: 32 },
    shots: [
      "在视频工作台同时展示带评论的轨迹与单帧标注，两枚角标分别显示 2 条与 1 条评论。",
      "用上一帧 / 下一帧在两个已知帧之间移动：单帧对象离开所在帧后角标消失，轨迹角标跟随对象。",
      "回到单帧对象所在帧，点击当前可见轨迹对象的评论角标。",
      "讨论面板切换到该对象的评论范围，只显示属于这条轨迹的两条评论，与角标总数一致。",
    ],
    editingNotes: [
      "角标数字是对象的评论总数；它包含其它成员以及本人已读的评论，不是当前账号的未读数。",
      "该母版只表达视频对象评论提示与讨论定位，不混入发送评论、AI 推理或帧删除。",
      "角标位置由当前帧的真实几何决定，不把角标停留在没有对象的帧位。",
    ],
  }),
  defineAsset({
    assetId: "discussion-mention-return",
    title: "任务留言提及与通知返回",
    theme: "通过 @ 选择器提及项目成员并从通知找回原评论",
    objective:
      "展示用真实 @ 选择器提及隔离的项目成员并发起任务留言，再以该成员自己的会话从通知中心打开“任务留言提到了你”，回到工作台并突出显示原评论。",
    duration: { minSeconds: 18, targetSeconds: 28, maxSeconds: 42 },
    shots: [
      "在任务留言输入框输入 @，从真实成员浮层中选择隔离的标注员。",
      "发送留言，读回服务端存储的提及对象与任务讨论来源。",
      "切换到该成员自己的会话，从通知中心打开“任务留言提到了你”。",
      "工作台回到原任务留言并突出显示该评论，通知同时标记为已读。",
    ],
    editingNotes: [
      "提及必须来自真实 @ 选择器（编辑器中的提及 chip），不用 API 直接写入 mentions。",
      "收件人是隔离的演示账号；流程结束后精确删除本条留言与该账号的提及通知。",
      "外部录制器按窗口采集，成员视图在同一窗口内取得；跨会话隔离核对在成片窗口之外执行。",
    ],
  }),
  defineAsset({
    assetId: "mask-slice-instances",
    title: "Mask 直线切割为两个实例",
    theme: "把一张已保存 Raster Mask 沿直线切成两个互斥实例",
    objective:
      "展示选中一个覆盖相邻两辆车的已保存 Raster Mask，用有向直线预览两块结果与像素数量、确认提交为两个实例，并依次选中它们。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 36 },
    shots: [
      "选中一个已保存的 Raster Mask，显示它同时覆盖前后相邻的两辆车。",
      "在 Mask 高级工具里选择「直线切割为两个实例」，沿两车结合部拖出有向直线。",
      "展示两块彩色预览与真实像素数量「面积 X → Y+Z px」，再提交 2 个实例。",
      "依次选中保留与新建的两个结果实例，两个已保存对象同时存在。",
    ],
    editingNotes: [
      "来源 Mask 由正式 mask-content API 上传，切割结果必须与落库的两块几何一致，不把预览当成结果。",
      "两块结果必须互斥且并集等于来源 Mask；撤销 / 重做只在母版之外用于核验同一对实例回来，不剪进片段。",
      "该素材只证明实例切分能力，不代表这些像素一定是真实目标类别。",
    ],
  }),
  defineAsset({
    assetId: "discussion-target-drafts",
    title: "讨论草稿按收件人各自保留",
    theme: "任务留言与标注草稿互不覆盖",
    objective:
      "展示在讨论输入框为当前任务写下草稿、选中一个已保存对象改写另一段草稿、只切换阅读范围后往返两个收件人，并在发送任务留言后于「任务留言」来源下读回。",
    duration: { minSeconds: 18, targetSeconds: 28, maxSeconds: 40 },
    shots: [
      "把右栏拖宽，在「当前任务」收件人下逐字输入「整体边界待复核」。",
      "选中一个已保存对象：发送目标跟随该对象，输入框切换到它自己的空草稿并输入第二段。",
      "只切换阅读范围（任务 / 标注 / 全部），两个收件人的草稿都保持各自原样。",
      "发送任务草稿，在「任务留言」来源标签下读回这条真实评论。",
    ],
    editingNotes: [
      "草稿按用户 / 项目 / 任务 / 目标分桶，只承诺同一登录会话内恢复；片段不做刷新，也不声明刷新后仍存。",
      "发送依赖真实 POST /feedbacks 响应，评论在母版之外按 id 精确删除。",
      "右栏加宽只是录制准备，不写回产品默认布局，也不改动已保存偏好。",
    ],
  }),
  defineAsset({
    assetId: "task-drawing-comment",
    title: "在题图上圈点并随评论发送",
    theme: "画布圈点说明不修改已保存标注",
    objective:
      "展示不选中任何标注时点「在题图上绘制」，在题图上圈出一辆尚未保存为标注的车辆、把笔触附加到输入框、不写正文直接发送，并打开这条只有绘图的评论。",
    duration: { minSeconds: 14, targetSeconds: 22, maxSeconds: 32 },
    shots: [
      "输入框停在「当前任务」且没有选中任何标注，点击「在题图上绘制」。",
      "在题图上按住拖动，圈出一辆尚未保存为标注的车辆。",
      "点「完成」把这段笔触附加到输入框（批注 · 1 条），不写正文直接发送。",
      "读回这条只有绘图的评论，并点开评论行把绘图固定叠加回画布。",
    ],
    editingNotes: [
      "圈点是画布批注，随评论发送，不写回任何标注；片段用真实服务端回读证明已保存标注的 id 与版本完全未变。",
      "发送不含正文，只带 canvas_drawing；评论在母版之外按 id 精确删除。",
      "圈点区域必须是当时没有已保存标注的对象，若夹具状态不符则流程直接失败而不是补拍。",
    ],
  }),
  defineAsset({
    assetId: "ai-prediction-import",
    title: "导入 AI 预标注",
    theme: "导入预测结果的人工复核与采纳",
    objective: "展示与真实车辆对齐的 AI 候选，并将其采纳为正式标注。",
    duration: { minSeconds: 8, targetSeconds: 10, maxSeconds: 20 },
    shots: [
      "从工作台加载到完整道路图像。",
      "显示来自导入预测的货车与轿车候选框。",
      "在详情列表中检查一个候选并执行采纳。",
      "展示候选数减少、人工标注数增加的落库结果。",
    ],
    editingNotes: [
      "该母版不混入人工画框，但必须保留人工采纳闭环。",
      "可剪掉加载骨架屏，但必须保留完整图像和全部导入框。",
    ],
  }),
  defineAsset({
    assetId: "ai-preannotate",
    title: "发起 AI 预标注",
    theme: "批量任务的 AI 预标注启动流程",
    objective: "展示选择配置、发起作业、核对结果并返回工作台的完整链路。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 25 },
    shots: [
      "选择处理范围和模型配置。",
      "发起作业，在历史列表中等待完成。",
      "打开 job 详情核对成功数，再进入工作台展示候选。",
    ],
    editingNotes: ["保留配置、运行、job 结果和工作台候选四个阶段。"],
  }),
  defineAsset({
    assetId: "sam-tools/smart-point",
    title: "智能点分割",
    theme: "单点提示生成对象轮廓",
    objective: "展示点击目标、AI 生成轮廓、选择 car 并落库的完整过程。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 25 },
    shots: [
      "启用智能点并在车辆内放置正点。",
      "展示生成的轮廓候选。",
      "选择 car 类别并展示已保存标注。",
    ],
    editingNotes: ["保留点击前的原始目标，便于对比 AI 结果。"],
  }),
  defineAsset({
    assetId: "sam-tools/smart-box",
    title: "智能框分割",
    theme: "框提示生成精细轮廓",
    objective: "展示粗框提示、像素级候选、car 类别确认与落库。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 25 },
    shots: [
      "启用智能框并拖出包围车辆的提示框。",
      "展示 AI 分割候选。",
      "选择 car 类别并展示已保存标注。",
    ],
    editingNotes: ["保留提示框到轮廓出现的完整因果关系。"],
  }),
  defineAsset({
    assetId: "sam-tools/exemplar",
    title: "Exemplar 示例分割",
    theme: "用示例目标引导相似对象检索",
    objective: "展示正示例框如何引导模型找到相似目标，并把结果落库。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 30 },
    shots: [
      "启用 Exemplar 并框选正示例。",
      "展示相似目标候选。",
      "选择 car 类别并展示已保存标注。",
    ],
    editingNotes: ["不要裁掉示例标记，它是理解结果的关键上下文。"],
  }),
  defineAsset({
    assetId: "smart-scribble",
    title: "Mask 正负笔迹精修",
    theme: "在已存 Mask 上通过笔迹修正边界",
    objective: "展示如何用正向与负向笔迹改善已有分割结果。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 30 },
    shots: ["展示原始 Mask。", "添加正负笔迹。", "展示精修候选与更新后边界。"],
    editingNotes: ["保留原 Mask、正负笔迹、精修候选和采纳后边界四个阶段。"],
  }),
  defineAsset({
    assetId: "ai-assisted-annotation",
    title: "Magic Box AI 辅助标注",
    theme: "AI 候选生成与人工确认",
    objective: "展示用 Magic Box 发起真实推理、检查候选并确认为标注。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 30 },
    shots: ["拖出 Magic Box 提示。", "展示推理中状态和候选。", "确认候选为正式标注。"],
    editingNotes: ["必须保留候选虚线到人工确认的状态变化。"],
  }),
  defineAsset({
    assetId: "review-reject",
    title: "质检批次审核与精准退回",
    theme: "从批次进度定位问题任务并形成可追溯的返工闭环",
    objective:
      "展示质检员从按项目分组的批次卡片进入待审列表，在审核工作台核对人工标注，以结构化原因退回问题任务，并回到批次概览确认待审计数已经更新。",
    duration: { minSeconds: 20, targetSeconds: 28, maxSeconds: 40 },
    shots: [
      "从质检审核 landing 开始，同时展示项目分组、批次封面、标注员、待审与通过进度。",
      "进入固定审核中批次，核对批次进度与待审任务摘要，再打开目标任务。",
      "在审核工作台选中人工标注并切换最终 / diff 视图，检查几何与 AI 原始结果的关系。",
      "选择“几何不准确”，填写明确返工要求后提交真实退回。",
      "返回批次列表，确认目标任务已从待审队列移除，批次卡片的待审进度同步减少。",
    ],
    editingNotes: [
      "该母版只表达单任务质检退回，不混入批量通过、整批退回或标注员接受返工。",
      "必须同时保留退回前后的批次待审计数、审核画布、结构化原因和成功反馈，不能只录对话框。",
      "录制调用正式审核 API；结束后只恢复固定截图任务，并精确删除本次反馈与通知，审计记录保持不可变。",
    ],
  }),
  defineAsset({
    assetId: "batch-bulk-actions",
    title: "批次批量操作",
    theme: "多选任务的集中处理",
    objective: "展示在批次页多选任务并执行批量操作。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 20 },
    shots: ["进入批次任务列表。", "勾选多个任务。", "执行批量动作并展示结果。"],
    editingNotes: ["保留勾选数量和操作结果。"],
  }),
  defineAsset({
    assetId: "ai-pre-variant-selector",
    title: "AI 预标注变体选择",
    theme: "模型与运行档位的联动配置",
    objective: "展示预标注页面中模型、变体和能力筛选的联动。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 20 },
    shots: ["打开模型选择。", "切换模型并观察档位选项。", "停留展示最终配置。"],
    editingNotes: ["画面需同时包含两个选择器，不要裁成局部特写。"],
  }),
  defineAsset({
    assetId: "pipeline-template-create",
    title: "创建公共 AI 编排模板",
    theme: "把车辆检测与属性分类组织成可复用的两阶段 DAG",
    objective:
      "展示为图像数据源配置 YOLO 车辆检测、限定车辆类别，再添加车辆属性分类阶段并将车型与颜色写回父框，最终保存为公共模板。",
    duration: { minSeconds: 18, targetSeconds: 24, maxSeconds: 30 },
    shots: [
      "从已加载的编排库开始，填写“车辆检测 → 车型与颜色”并确认公共可见范围。",
      "选择 YOLO 目标检测作为源模型，并限定 car、bus、truck 三类车辆。",
      "从源模型添加子阶段，选择专用车辆属性分类，并限定父框类别与车型、颜色写回键。",
      "保存后展示成功反馈，以及命名编排库中新出现的公共两阶段模板。",
    ],
    editingNotes: [
      "该母版只表达模板创建，不混入套用项目、运行预标或作业历史；这些能力各自独立录制。",
      "必须让数据源、源模型、下游分类节点及“公共 · 2 阶段”结果可读，不能只录表单局部。",
      "录制结束后精确删除本次创建的模板，避免后续素材出现重复记录。",
    ],
  }),
  defineAsset({
    assetId: "pipeline-apply-project",
    title: "套用并执行公共 AI 编排",
    theme: "把公共两阶段模板复制为项目默认编排并运行真实车辆推理",
    objective:
      "展示从项目预标页选择车辆检测与属性分类公共模板，copy-on-write 套用为项目默认，再进入工作台执行同一两阶段编排并查看带车型、颜色属性的车辆候选。",
    duration: { minSeconds: 18, targetSeconds: 28, maxSeconds: 90 },
    shots: [
      "从已加载的 AI 预标项目列表进入真实道路场景项目。",
      "在命名编排库选择“车辆检测 → 车型与颜色”公共两阶段模板，并套用为项目默认。",
      "展示套用成功反馈与项目内“车辆检测 → 车型与颜色 · 2 阶段”默认编排标识。",
      "进入已预热的图片工作台，从当前题 AI 运行项目默认两阶段编排。",
      "等待真实 YOLO 检测与车辆属性分类完成，选中同时带车型和颜色的车辆候选。",
    ],
    editingNotes: [
      "该母版不混入模板创建、批次勾选或候选采纳；模板创建和候选审阅已有独立资产。",
      "套用只是 copy-on-write 绑定，实际执行必须通过工作台“按项目编排 · 2 阶段”入口完成，不能伪装成套用后自动批跑。",
      "候选必须来自真实 YOLO 与 onnxtools 后端，并同时携带 vehicle_type、color；录制结束后精确清理预测、作业和两条临时编排。",
    ],
  }),
  defineAsset({
    assetId: "jobs-retry-recovery",
    title: "AI 失败作业恢复",
    theme: "从失败摘要到真实重试结果的完整闭环",
    objective:
      "展示项目管理员打开失败的 RapidOCR 作业，核对错误和可重试项，沿用原模型配置重新推理，确认新作业已完成后进入工作台审阅 OCR 候选。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 45 },
    shots: [
      "从已加载的失败 Jobs 列表定位 OCR 作业。",
      "打开详情，展示错误摘要、1 条失败项和可重试入口。",
      "点击重试，保留排队反馈与真实 RapidOCR 执行窗口。",
      "切换到全部状态，展示新的 prediction_retry 作业已完成且失败数为 0。",
      "从完成的重试行进入工作台，选中一个真实 OCR 候选展示结果。",
    ],
    editingNotes: [
      "原批量作业保留失败终态作为历史快照；恢复成功以新增的已完成 prediction_retry 行表达，不得伪造原行翻转。",
      "重试必须命中真实 RapidOCR 后端并产生可审阅文字候选，不使用前端 stub 或预写成功数据。",
      "失败夹具、重试作业、预测和通知均按录制标记精确清理。",
    ],
  }),
  defineAsset({
    assetId: "model-market-runtime-pool",
    title: "模型服务池运行时观测",
    theme: "从服务池摘要下钻到多实例路由、资源指标与详情",
    objective:
      "展示超级管理员核对强制路由模式、数据新鲜度和两个服务池，展开车辆检测池比较两台实例的权重、接流状态、当前并发与 GPU 驻留，并切换实例详情核对完整运行证据。",
    duration: { minSeconds: 15, targetSeconds: 22, maxSeconds: 30 },
    shots: [
      "从已加载的运行时观测页开始，展示强制路由、3/3 可路由实例、无异常池和完整数据来源摘要。",
      "展开数据来源，确认拓扑、路由账本、健康、GPU 和模型驻留均为新鲜状态。",
      "展开车辆检测多实例池，同时展示 70/30 权重、接流状态、当前并发、GPU 驻留和“暂无路由指标”的真实未知语义。",
      "依次打开车辆检测 A 与 B 的详情 Sheet，比较路由容量、GPU claim、模型版本、显存和 cache 观测。",
    ],
    editingNotes: [
      "该母版只表达运行时观测，不混入注册实例、项目绑定修改、停流或卸载等管理动作。",
      "观测数据来自录制专用确定性快照；内部 URL、真实 GPU UUID 和实例 ID 必须使用脱敏演示值。",
      "摘要、展开后的两台实例和两个详情 Sheet 都必须完整可读，不能只录卡片点击动作。",
      "当前共享路由计数器尚未接入，selection/rejection、P95、错误率和最近选择必须保持 null，并显示“暂无路由指标”，不得伪造数值。",
    ],
  }),
  defineAsset({
    assetId: "project-ml-routing",
    title: "项目多后端能力路由",
    theme: "批量主后端与交互工具按能力自动分流",
    objective:
      "展示项目启用 YOLO 批量后端并即时设为主后端，随后进入工作台确认当前题批量 AI 使用 YOLO，而 Smart Point 自动路由到支持 point 的 SAM3 交互后端。",
    duration: { minSeconds: 18, targetSeconds: 25, maxSeconds: 32 },
    shots: [
      "从项目 ML 模型页打开管理面板，确认 SAM3 已启用并勾选 yolo-backend。",
      "返回双后端清单，把 yolo-backend 即时设为项目主后端并核对主后端标识。",
      "进入工作台打开当前题 AI，确认批量线默认选择 yolo-backend（项目主后端）。",
      "切换到 Smart Point，确认交互工具的引擎自动解析为支持 point 的 SAM3 后端。",
    ],
    editingNotes: [
      "当前产品没有独立的“能力路由保存”表单；启用和主后端变更均即时生效，素材不得伪造保存按钮。",
      "该母版只表达项目多后端启用与能力分流，不混入模型注册、实际分割或批量作业执行。",
      "必须同时保留 YOLO 批量主后端与 SAM3 交互引擎两个结果画面，避免把主后端误解为唯一后端。",
      "设置页使用录制专用的脱敏读取视图，把内部 backend URL 显示为 ml.example.invalid；启用与主后端写入仍走正式 API。",
      "流程结束后重建 screenshots profile，恢复主后端与项目启用关系。",
    ],
  }),
  defineAsset({
    assetId: "model-market-video-pool",
    title: "视频追踪独立模型池",
    theme: "区分视频 tracker 的独立显存池与普通图片推理池",
    objective:
      "展示超级管理员从运行时服务池进入 SAM 3 视频追踪实例，核对视频池容量、常驻权重、活跃会话与 GPU 驻留，并展开视频变体区确认独立预热入口。",
    duration: { minSeconds: 12, targetSeconds: 18, maxSeconds: 28 },
    shots: [
      "从已加载的运行时观测页定位视频追踪独立服务池，并与车辆检测、OCR 图片池并列对比。",
      "展开视频服务池，核对 SAM 3 视频追踪实例的接流状态、当前并发与 GPU 驻留。",
      "打开实例详情，展示视频池 1/3、2 个活跃会话、SAM 3.1 权重和独立 residency 证据。",
      "展开模型预热与变体区域，确认视频追踪权重已在显存，且预热入口明确属于视频池。",
    ],
    editingNotes: [
      "该母版只表达视频模型池观测与预热入口，不混入视频任务发起或候选审阅；任务历史已有独立素材。",
      "视频池与图片池必须同时可辨，不能把图片 pool 的 loaded key 冒充 tracker 驻留状态。",
      "观测数据使用录制专用脱敏快照；内部 URL、真实 GPU UUID 和实例 ID 不进入素材。",
    ],
  }),
  defineAsset({
    assetId: "video-timeline-prediction-navigation",
    title: "AI 预测密度与帧导航",
    theme: "沿时间轴定位并逐帧核对视频车辆候选",
    objective:
      "展示真实 YOLO 多帧车辆候选在时间轴上的预测密度分布，并使用上一帧 / 下一帧导航连续核对画布中的公交车与卡车候选框。",
    duration: { minSeconds: 12, targetSeconds: 17, maxSeconds: 24 },
    shots: [
      "从已完成多帧推理的候选审阅态开始，同时展示画布车辆候选、当前帧与折叠时间轴摘要。",
      "展开时间轴详情，完整呈现 violet AI 预测密度轨及其在整段视频中的分布。",
      "连续点击下一个有预测的帧，核对帧号推进与公交车 / 卡车候选框同步变化。",
      "再使用上一个有预测的帧回看，证明导航只落在实际存在预测的帧。",
    ],
    editingNotes: [
      "真实 YOLO 多帧候选在录制窗口前生成；成片只表达预测密度与帧导航，不混入模型配置或推理发起。",
      "必须保留 AI 待审计数、violet 密度轨、前后导航按钮、帧号和画布车辆候选，不能只裁时间轴局部。",
      "录制结束后在裁切区间外精确清理候选；不把本素材的导航演示误写成采纳结果。",
    ],
  }),
  defineAsset({
    assetId: "model-market-runtime-partial-failure",
    title: "运行时数据源部分失败",
    theme: "单一观测来源退化时保留其它可信状态",
    objective:
      "展示路由账本连接超时后，模型市场仍保留服务池与其它四个新鲜数据源，并通过部分可用告警、上次更新时间、失败原因和退避信息保留真实的不确定性。",
    duration: { minSeconds: 9, targetSeconds: 12, maxSeconds: 20 },
    shots: [
      "从已加载的运行时观测页开始，同时展示 4/5 数据新鲜与两个仍可读服务池。",
      "展开数据来源，展示“部分数据来源失败”及路由账本超时 / 30 秒退避原因。",
      "悬停新鲜的拓扑来源，核对最新更新时间。",
      "悬停陈旧的路由账本来源，核对错误、上次成功时间与退避信息。",
    ],
    editingNotes: [
      "该母版只表达运行时数据可信度，不混入实例注册、服务池编辑、GPU 管理或实例详情。",
      "必须保留一个 stale/error 来源与至少一个 fresh 来源，不得把部分失败演示成全局中断。",
      "观测快照使用录制专用脱敏固定值；页面、视图模型、部分失败告警与 Tooltip 均走真实产品实现。",
    ],
  }),
  defineAsset({
    assetId: "model-market-gpu-resource-overview",
    title: "GPU 资源就绪性与阻断实例",
    theme: "从全局期望模式下钻到单卡预算、运行时账本和受影响实例",
    objective:
      "展示 GPU 仲裁全局期望为 enforce 时，正常卡与账本未就绪卡仍能分别呈现静态声明、运行时 committed、队列、Lease、desired → effective 和受影响实例。",
    duration: { minSeconds: 10, targetSeconds: 14, maxSeconds: 22 },
    shots: [
      "从已加载的注册管理服务池页开始，保留整体统计。",
      "切换到 GPU 资源，核对运行时未就绪、全局 enforce、Observe 就绪与 Enforce 未就绪。",
      "对比正常卡与 blocker 卡的可分配显存、声明预算、committed、队列、Lease 和模式。",
      "展开 blocker 卡，查看受影响的车辆检测 L4 实例与所属服务池。",
    ],
    editingNotes: [
      "观测数据使用录制专用脱敏固定快照；资源表、两根预算进度条、诊断聚合和实例反查均走真实产品实现。",
      "该母版只表达 GPU 资源治理，不混入后端注册、服务池编辑或实例详情。",
    ],
  }),
  defineAsset({
    assetId: "platform-overview",
    title: "超级管理员平台概览",
    theme: "从平台规模与项目分布下钻到注册趋势、模型成本、审计活动和项目组合",
    objective:
      "展示超级管理员如何在一个全局视图中核对用户、项目、任务和标注规模，理解项目与角色分布，并继续查看注册来源、ML 后端成本、近期业务事件和全平台项目。",
    duration: { minSeconds: 16, targetSeconds: 22, maxSeconds: 30 },
    shots: [
      "从已加载的平台概览开始，展示四项 KPI、项目状态分布和用户角色分布。",
      "向下查看 30 天注册来源以及邀请 / 开放注册构成。",
      "核对 ML 后端在线数、调用量、延迟、失败率和成本。",
      "查看近期审计活动中的项目、AI 和模型服务事件。",
      "落到全平台项目表，对比图片、视频和 3D 点云项目的负责人、成员与状态。",
    ],
    editingNotes: [
      "聚合数据、审计账号和项目名称使用录制专用脱敏固定快照；页面、卡片、图表和滚动均走真实产品实现。",
      "该母版只表达平台运营概览，不混入独立的系统健康页、项目操作菜单或成员绩效下钻。",
      "滚动保留页面层级和连续性，后期可按顶部 KPI、模型成本、审计活动和项目组合拆成独立短片。",
    ],
  }),
  defineAsset({
    assetId: "project-actions-menu",
    title: "项目高频操作菜单",
    theme: "从项目列表进入预测导入、标注导出和配置复用",
    objective:
      "展示项目管理员在保留项目上下文的情况下，从同一个更多操作菜单找到导出标注数据、复制项目配置和导入预测三个高频入口。",
    duration: { minSeconds: 9, targetSeconds: 12, maxSeconds: 20 },
    shots: [
      "从已加载的项目列表开始，明确当前道路图像项目。",
      "打开该项目的更多操作菜单，同时展示三个高频入口及其分组关系。",
      "依次悬停导出标注数据、复制项目配置和导入预测，保留指针焦点。",
    ],
    editingNotes: [
      "该母版只表达入口发现，不打开向导或执行写操作；预测导入、后台导出和复制创建均由各自独立素材说明。",
      "必须保留项目行、项目编号和数据类型语境，不能只裁一个失去来源的浮动菜单。",
    ],
  }),
  defineAsset({
    assetId: "jobs-bell-active",
    title: "后台任务混排与关键动作",
    theme: "在一个顶栏面板中核对预标、导入和导出的不同状态与下一步动作",
    objective:
      "展示批量预标与预测导入的实时进度和取消入口，同时展示已完成双格式导出的 ZIP 摘要与下载入口。",
    duration: { minSeconds: 10, targetSeconds: 14, maxSeconds: 22 },
    shots: [
      "从已加载的项目页开始，顶栏后台任务角标显示两个进行中作业。",
      "展开后台任务面板，同时展示批量预标、预测导入和已完成数据导出。",
      "核对两条进行中进度与批量预标的取消入口。",
      "定位已完成导出的目标格式、ZIP 文件数、大小和下载入口。",
    ],
    editingNotes: [
      "作业列表使用录制专用脱敏只读快照；面板、筛选、进度、取消和下载控件均走真实产品实现。",
      "该母版只表达全局任务铃，不进入完整 Jobs 页面，也不实际取消作业或下载伪造链接。",
    ],
  }),
  defineAsset({
    assetId: "video-tracker-job-states",
    title: "视频追踪任务状态与工作台回跳",
    theme: "从追踪执行态进入候选审阅，并回到对应视频任务继续处理",
    objective:
      "展示视频追踪任务的运行中、待审阅、已采纳和已丢弃状态，使用项目与状态筛选定位待审记录，并返回对应视频工作台。",
    duration: { minSeconds: 12, targetSeconds: 17, maxSeconds: 26 },
    shots: [
      "从已加载的视频任务页开始，同时对比四种关键状态及其聚合计数。",
      "选择城市公交视频项目，证明项目筛选作用于专用追踪任务记录。",
      "筛选待审阅状态，核对模型、帧范围、方向与任务来源。",
      "通过该任务的返回入口进入对应视频工作台，并保留返回任务页的路径。",
    ],
    editingNotes: [
      "任务数据使用录制专用脱敏只读快照；状态、筛选、表格和工作台导航均走真实产品实现。",
      "该母版只表达视频任务历史与工作台回跳，不在工作台内发起追踪或审阅候选，相关操作由独立素材说明。",
      "开头保留四状态同屏画面，后期可独立裁成文档封面或状态对比短片。",
    ],
  }),
  defineAsset({
    assetId: "background-export-download",
    title: "后台多格式导出与下载",
    theme: "从导出配置到可校验 ZIP 产物的异步闭环",
    objective:
      "展示为真实图像项目同时选择 COCO 和 AAP JSON，发起后台导出，在任务铃中查看等待、运行与完成状态，核对文件数和大小后下载并验证 ZIP 内容。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 32 },
    shots: [
      "从已加载的项目列表打开“导出标注数据”，保留项目语境。",
      "同时选择 COCO 和 AAP JSON，展示双格式打入同一 ZIP 的配置摘要。",
      "开始导出后打开后台任务，展示等待和运行中进度。",
      "任务完成后核对项目、两种格式、ZIP 文件数与产物大小。",
      "点击下载，并由录制验收校验 ZIP 同时包含 coco/ 与 aap_json/ 目录。",
    ],
    editingNotes: [
      "该母版只表达异步导出与产物下载，不混入预测导入、清理预测或其他项目管理操作。",
      "导出作业、进度、MinIO 产物和预签名下载链接均由正式后端产生；录制器不伪造成功状态。",
      "录制使用指向隔离截图库的专用导出执行器，保留短暂运行态供任务铃轮询捕获；这是后台真实状态，不是视频慢放。",
      "下载后必须解析真实 ZIP 目录；流程结束后精确删除录制作业、通知、缓存行与对象存储产物。",
    ],
  }),
  defineAsset({
    assetId: "project-create-existing-resources",
    title: "创建项目并复用已有资源",
    theme: "在六步向导中关联已有数据集与团队成员",
    objective:
      "展示项目管理员创建图片质检项目，配置车辆类别，再从平台现有资源中选择一个道路数据集、一位标注员和一位审核员，最后核对创建摘要。",
    duration: { minSeconds: 14, targetSeconds: 19, maxSeconds: 32 },
    shots: [
      "从已加载的项目总览打开新建项目向导，填写明确的车辆质检项目名。",
      "保留图片与矩形框工具语境，为 bbox 工具单位新增 car 类别。",
      "依次通过属性和 AI 接入步骤创建项目，不额外启用与本母版无关的能力。",
      "在数据步选择已导入的道路数据集并执行真实关联，不现场上传。",
      "在成员步同时选择现有标注员与审核员，不现场邀请新账号。",
      "成功页展示项目编号，并核对已关联 1 个数据集、已添加 2 位成员。",
    ],
    editingNotes: [
      "该母版只表达创建项目与复用已有资源，不混入数据上传、账号邀请、工作台标注或项目删除画面。",
      "第 5 步和第 6 步必须保留可跳过语义与现有资源清单，便于后期解说“关联 / 选择”而非“上传 / 邀请”。",
      "创建、关联数据集和添加成员全部调用正式 API；录制结束后再通过正式级联删除 API 精确清理本次项目。",
    ],
  }),
  defineAsset({
    assetId: "ocr-real-scene",
    title: "真实场景 OCR 推理",
    theme: "当前任务的文字检测与识别",
    objective: "展示发起 RapidOCR 推理、等待返回并呈现文字标注结果。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 90 },
    shots: ["展示原始文档或场景图。", "发起 OCR 并展示运行状态。", "展示检测框与识别文本。"],
    editingNotes: ["可压缩推理等待时间，但需保留明确的运行中状态。"],
  }),
  defineAsset({
    assetId: "current-task-image-inference",
    title: "当前题图片编排推理",
    theme: "项目编排驱动的单题推理与人工采纳",
    objective: "展示运行已保存的项目编排、检查候选几何与置信度，并把一个候选采纳为正式标注。",
    duration: { minSeconds: 15, targetSeconds: 19, maxSeconds: 90 },
    shots: [
      "从已完成加载的真实 OCR 图片开始，打开当前题 AI。",
      "运行项目已保存的 OCR 编排，并保留明确的推理中状态。",
      "展示文字多边形候选、识别文本、待审计数和置信度。",
      "选中一个有意义的候选并采纳，展示候选减少且正式标注增加。",
    ],
    editingNotes: [
      "该母版不包含登录、首屏空白或项目编排编辑；只展示当前题的执行与人工接管。",
      "不得批量采纳全部候选，保留其余待审项才能清楚呈现候选到正式标注的状态变化。",
    ],
  }),
  defineAsset({
    assetId: "current-frame-video-inference",
    title: "视频当前帧车辆推理",
    theme: "单帧车辆检测、人工采纳与帧作用域确认",
    objective:
      "展示在视频目标帧运行真实车辆检测，采纳一个正确候选，并通过相邻帧确认结果只属于发起推理的当前帧。",
    duration: { minSeconds: 16, targetSeconds: 22, maxSeconds: 90 },
    shots: [
      "从已完成解码的行车视频定位到车辆清晰的目标帧。",
      "打开当前题 AI，选择专用车辆检测模型并运行当前帧推理。",
      "展示与真实车辆对齐的帧级候选、类别、置信度和帧号。",
      "采纳中间卡车候选，切换到相邻帧确认候选与人工框均不跨帧，再返回来源帧核对已落库结果。",
    ],
    editingNotes: [
      "该母版只展示单帧推理，不混入整段追踪、项目编排或人工画框。",
      "时间轴、帧号和右侧当前帧计数必须同屏可读，否则无法证明帧作用域。",
    ],
  }),
  defineAsset({
    assetId: "secondary-inference-attribute",
    title: "已确认文字区域二次推理",
    theme: "对已有文字标注补写 OCR 属性并人工校正",
    objective:
      "展示选中已落库文字区域，运行真实裁剪 OCR，把识别文本、方向和语言写回同一标注，再人工修正文案并确认属性来源变化。",
    duration: { minSeconds: 14, targetSeconds: 20, maxSeconds: 90 },
    shots: [
      "从已完整加载的 OCR 图片开始，选中准确覆盖‘强力去污 符合国标’的已确认文字区域。",
      "在二次推理面板选择 RapidOCR 文本识别原子模型并发起真实裁剪推理。",
      "展示识别文本、方向和语言写回同一标注，三个属性均带有 AI 来源标记。",
      "将识别文本人工修正为‘强力去污，符合国标’并保存，展示文本来源标记消失而其它 AI 属性仍保留。",
    ],
    editingNotes: [
      "该母版只展示已确认标注的属性二次推理，不混入整图候选、子框检测或人工绘制父区域。",
      "父区域、二次推理工具条和右侧属性面板必须同屏；人工修改后至少保留一个 AI 来源标记，以清楚表达字段级混合溯源。",
    ],
  }),
  defineAsset({
    assetId: "rotated-bbox",
    title: "旋转框绘制",
    theme: "有方向目标的框选与角度调整",
    objective: "展示绘制旋转框、拖动旋转手柄并落库。",
    duration: { minSeconds: 8, targetSeconds: 11, maxSeconds: 22 },
    shots: ["启用旋转框工具并绘制。", "展示选中框与手柄。", "旋转到目标方向并展示结果。"],
    editingNotes: ["需保留旋转前后两个稳定状态。"],
  }),
  defineAsset({
    assetId: "bbox-draw",
    title: "人工车辆矩形标注",
    theme: "只使用人工画框完成单个车辆标注",
    objective: "展示标注员选择矩形工具、沿真实轿车拖框、选择 car 类别并完成保存。",
    duration: { minSeconds: 4, targetSeconds: 4.5, maxSeconds: 8 },
    shots: [
      "稳定展示无人工框的道路图像，AI 层已隐藏。",
      "启用矩形工具，沿真实轿车边界完整拖出框。",
      "在类别弹层选择 car，稳定展示已保存的人工框和缩放手柄。",
    ],
    editingNotes: [
      "该母版只表达人工画框，不得混入登录、提交质检或 AI 导入标注。",
      "保留完整拖拽轨迹、类别选择和已保存结果，操作必须保持正常实时速度。",
      "不得用慢速鼠标、重复动作或过长静止画面填充时长。",
    ],
  }),
  defineAsset({
    assetId: "polyline-draw",
    title: "折线绘制",
    theme: "道路或线性结构的逐点标注",
    objective: "展示逐点创建折线并完成提交。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 20 },
    shots: ["启用折线工具。", "按路径逐点放置顶点。", "完成折线并展示节点。"],
    editingNotes: ["鼠标路径和顶点出现顺序是素材核心。"],
  }),
  defineAsset({
    assetId: "polygon-draw",
    title: "多边形绘制",
    theme: "不规则目标的轮廓标注",
    objective: "展示逐点勾勒目标外轮廓并闭合多边形。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 20 },
    shots: ["启用多边形工具。", "沿目标轮廓逐点勾勒。", "闭合图形并展示填充结果。"],
    editingNotes: ["保留至少三个顶点的创建过程。"],
  }),
  defineAsset({
    assetId: "polygon-boundary-trace",
    title: "沿已有边界追踪",
    theme: "相邻多边形共享同一条已保存边界",
    objective:
      "展示在已保存来源多边形旁边补全相邻区域：在来源边界上选起止点、对比顺逆时针两条路径、确认一条后闭合保存，来源对象保持不变。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 36 },
    shots: [
      "展示已保存的来源多边形与旁边待补全的相邻区域。",
      "在来源边界上依次点击起点与终点，显示两条候选路径的点数与较短标记。",
      "切换方向对比后确认一条路径，草稿沿来源边界补全并核对真实来源状态。",
      "闭合保存相邻多边形，两个对象同时存在且共享边界，来源版本与几何不变。",
    ],
    editingNotes: [
      "共享边界来自来源轮廓的真实顶点，预览路径与落库几何必须一致。",
      "该素材只证明共享边界追踪能力，不表示相邻区域一定是真实目标类别。",
      "保留两个方向的真实对比，不要剪成只剩确认动作。",
    ],
  }),
  defineAsset({
    assetId: "polygon-slice-undo",
    title: "多边形切割与撤销重做",
    theme: "把已保存多边形切成两块并撤销、重做一次",
    objective:
      "展示选中一个已保存的简单多边形、用穿越切线预览两块结果、确认落库，再用撤销与重做证明同两个已保存对象回来。",
    duration: { minSeconds: 18, targetSeconds: 26, maxSeconds: 38 },
    shots: [
      "选中一个已保存、未锁定的简单多边形，并从右键菜单打开「切割多边形」。",
      "从对象外逐点绘制穿越切线，预览显示两块真实结果。",
      "确认切割，展示保留的来源与新建对象。",
      "撤销回到一个来源对象，再重做让同两个已保存对象回来。",
    ],
    editingNotes: [
      "撤销与重做各一次，用来证明真实恢复接口；冲突、期限与失败路径不放进本片段。",
      "预览的两块几何必须与落库的两个对象一致，不得用重放片段替代真实恢复。",
    ],
  }),
  defineAsset({
    assetId: "mask-draw",
    title: "Mask 笔刷涂抹",
    theme: "像素级目标区域绘制",
    objective: "展示使用笔刷累积绘制 Mask 并完成标注。",
    duration: { minSeconds: 7, targetSeconds: 10, maxSeconds: 22 },
    shots: ["启用 Mask 笔刷。", "用多次笔画覆盖目标。", "完成并展示半透明 Mask。"],
    editingNotes: ["不要过度加速笔画，需让观众看清累积覆盖过程。"],
  }),
  defineAsset({
    assetId: "candidate-keyboard-review",
    title: "AI 候选键盘审阅",
    theme: "用快捷键高效采纳或驳回候选",
    objective: "展示候选聚焦、键盘决策与自动前进到下一个候选。",
    duration: { minSeconds: 10, targetSeconds: 14, maxSeconds: 30 },
    shots: ["展示多个 AI 候选。", "用键盘采纳或驳回当前候选。", "焦点自动前进并继续审阅。"],
    editingNotes: ["需保留至少两次候选切换，才能体现自动前进。"],
  }),
  defineAsset({
    assetId: "candidate-review-lifecycle",
    title: "AI 候选完整审阅生命周期",
    theme: "逐条跳过、采纳、驳回并自动推进候选焦点",
    objective:
      "展示三个真实车辆候选依次经历跳过、采纳和驳回，并用最终待审与已确认计数证明每种决策的结果。",
    duration: { minSeconds: 16, targetSeconds: 22, maxSeconds: 28 },
    shots: [
      "从三个与车辆准确对齐的 AI 候选和 3 条待审计数开始。",
      "选中第一条后按 Tab 跳过，候选仍保留且待审计数不变。",
      "采纳第二条，展示已确认增加、待审减少，并自动聚焦第三条。",
      "驳回第三条，展示焦点自动回到最初跳过的候选，最终为 1 条待审、1 条已确认。",
    ],
    editingNotes: [
      "必须保留初始 3/0、采纳后 2/1 和最终 1/1 三组计数，不能只展示快捷键动作。",
      "三个候选必须分别命中真实车辆；跳过项在结尾仍需可见并处于选中状态。",
      "该母版只表达图片候选审阅，不混入推理发起、导入预测或任务提交。",
    ],
  }),
  defineAsset({
    assetId: "video-track",
    title: "视频时序标注工作台",
    theme: "视频帧、轨迹和时间轴的协同工作流",
    objective: "展示视频播放、逐帧导航、轨迹和时间轴的整体关系。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 30 },
    shots: ["展示视频画布和时间轴。", "播放或逐帧移动观察标注。", "展示轨迹在多帧中的延续。"],
    editingNotes: ["保留时间轴和画布同屏，避免失去时序语义。"],
  }),
  defineAsset({
    assetId: "video-track-batch-propagate",
    title: "双轨迹批量 AI 延展",
    theme: "一次选中两条现有轨迹并在同一作业中各自追踪",
    objective:
      "展示左右两辆公交车轨迹多选后，画布浮卡与右栏批量工具条同时响应，再以一个真实 SAM3 作业批量延展、跨帧复核并回填两条原轨迹。",
    duration: { minSeconds: 18, targetSeconds: 26, maxSeconds: 45 },
    shots: [
      "在 F0 同时显示两条与完整公交车对齐的 bus 轨迹。",
      "用 Ctrl 多选两条轨迹，停留展示高亮框、画布浮动批量卡和右栏“已选 2 条轨迹”工具条。",
      "打开批量延展面板，核对“批量延展 2 条轨迹 · bus”，选择真实点框追踪模型与 10 帧范围。",
      "一次作业生成两个目标的 F0–F10 候选，将接受窗口改为 F1–F10 保留人工 F0，并拖动时间轴复核两辆车。",
      "一次接受后展示两条原轨迹都已回填 F1–F10，没有新建多余轨迹。",
    ],
    editingNotes: [
      "这是“多选批量延展”的单一完整链路；候选审阅是该作业的下游闭环，不属于混入第二个主题。",
      "必须保留多选态至少 1.5 秒，让右栏工具条与画布浮卡都能被后期单独截取。",
      "追踪候选必须同时对齐两辆完整公交车，不得把中间卡车、车灯或雨刮器当作源目标。",
      "录制结束后按作业 ID 与两条源标注 ID 精确清理，不依赖整个项目重建。",
    ],
  }),
  defineAsset({
    assetId: "video-propagate-track-vs-copy",
    title: "AI 延展与几何复制对比",
    theme: "在同一真实轨迹上辨别两种跨帧传播机制",
    objective:
      "展示同一条中间完整卡车轨迹纯几何复制到 F30 后明显偏离真实目标，撤销后用真实 SAM3 作业延展、跨帧复核候选并回填原轨迹。",
    duration: { minSeconds: 18, targetSeconds: 28, maxSeconds: 36 },
    shots: [
      "从 F0 选中与完整中间卡车对齐的单条 truck 轨迹，同时展示“延展此轨迹”与“复制后续”。",
      "选择纯几何复制 F0→F30，连续拖动时间轴，在 F30 展示固定框与已变大、移动卡车的明显偏离。",
      "一次撤销恢复单个人工源关键帧，返回 F0 后从同一轨迹发起真实 SAM3 延展作业。",
      "在 F0–F30 候选审阅态中连续拖动时间轴，展示 AI 框跟随同一辆卡车的位移与尺寸变化。",
      "仅采纳 F1–F30 候选，保留人工 F0，忽略剩余种子帧候选并展示回填后的 F30 轨迹。",
    ],
    editingNotes: [
      "对比必须使用同一条中间卡车轨迹和同一 F0→F30 范围，不能用不同目标制造伪差异。",
      "纯几何段必须显示 F30 的实际偏离；AI 段必须显示完整卡车候选跟随、审阅决策与最终回填，不能只录两个入口按钮。",
      "F30 AI 框必须保持整车级宽高且随车辆接近明显变大，自动断言会拒绝雨刮器、车灯等局部零件。",
      "撤销仅用于回滚纯几何复制；AI 作业必须通过候选审阅闭环，两种机制的边界要保持清楚。",
      "录制结束后按轨迹 ID 与作业 ID 精确清理，不依赖整个项目重建。",
    ],
  }),
  defineAsset({
    assetId: "video-timeline-zoom",
    title: "时间轴锨点缩放",
    theme: "以指针为中心放大、缩小与复位",
    objective: "展示长视频时间轴的精确导航。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 25 },
    shots: ["展示初始时间轴范围。", "在指针附近连续缩放。", "复位到全局范围。"],
    editingNotes: ["鼠标指针与时间刻度必须清晰可见。"],
  }),
  defineAsset({
    assetId: "video-chapter",
    title: "视频章节创建与调整",
    theme: "在时间轴上圈选并修改章节范围",
    objective: "展示拖拽创建章节、悬停显示手柄和调整边界。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 30 },
    shots: ["在时间轴拖出章节区间。", "悬停展示章节边界手柄。", "拖动手柄调整起止范围。"],
    editingNotes: ["可派生为“创建章节”和“调整章节”两条短片，但母版保留完整因果链。"],
  }),
  defineAsset({
    assetId: "video-tracker-range",
    title: "AI 追踪范围刷选",
    theme: "在时间轴上限定追踪帧段",
    objective: "展示双公交车框种子、Shift 刷选范围、生成候选并分别人工确认的完整链路。",
    duration: { minSeconds: 12, targetSeconds: 18, maxSeconds: 90 },
    shots: [
      "为真实左右公交车分别设置 bus 类别和整车框种子。",
      "在时间轴 Shift 刷选 F0→F20 的追踪范围并发起双目标作业。",
      "核对两条完整公交车候选轨迹，再人工采纳并保存读回。",
    ],
    editingNotes: ["保留修饰键提示或对应 UI，便于理解刷选操作。"],
  }),
  defineAsset({
    assetId: "video-tracker-cross-frame-points",
    title: "跨帧多正点追踪",
    theme: "用跨帧正点同时追踪两个目标",
    objective: "展示左右两辆公交车分别设置 F0/F4 正点、同时追踪、跨帧核对与采纳。",
    duration: { minSeconds: 12, targetSeconds: 18, maxSeconds: 90 },
    shots: [
      "为目标 1 的左侧公交车放置 F0/F4 正点。",
      "点击“+ 新目标”，在停靠面板旁为目标 2 的右侧公交车放置 F0/F4 正点。",
      "同时生成两个候选后拖动时间轴，跨帧核对 AI 追踪框，再整批采纳。",
    ],
    editingNotes: ["保留 F0→F4→F0 的种子切帧过程，以及候选阶段从前段拖到后段再回看的时间轴镜头。"],
  }),
  defineAsset({
    assetId: "video-tracker-positive-negative",
    title: "正负点修正追踪",
    theme: "用正负点同时约束两个追踪目标",
    objective: "展示左右两辆公交车分别设置跨帧正负点、同时追踪、跨帧核对与采纳。",
    duration: { minSeconds: 13, targetSeconds: 19, maxSeconds: 90 },
    shots: [
      "为目标 1 的左侧公交车放置 F0/F4 正点和紧邻车身的红色负点。",
      "点击“+ 新目标”，为目标 2 的右侧公交车重复正负点约束。",
      "同时生成两个候选后拖动时间轴，跨帧核对 AI 追踪框，再整批采纳。",
    ],
    editingNotes: [
      "保留正负点颜色、Alt 负点操作、面板中的点数和候选框随时间轴移动的镜头；左侧公交车的正点必须落在车身上部与下部，避开挡风玻璃，避免把车窗当成轨迹。",
    ],
  }),
  defineAsset({
    assetId: "video-tracker-box-seed",
    title: "双目标整车框追踪",
    theme: "用完整目标框同时约束两条车辆轨迹",
    objective: "展示左右两辆公交车分别设置 F0 整车框、同时追踪、跨帧核对与采纳。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 90 },
    shots: [
      "沿左侧公交车完整车身绘制目标 1 的 F0 框。",
      "点击“+ 新目标”，沿停靠面板旁的右侧公交车绘制目标 2 的 F0 框。",
      "同时生成两个候选后拖动时间轴，跨帧核对 AI 追踪框，再整批采纳。",
    ],
    editingNotes: [
      "完整保留整车框的拖拽落下过程，以及候选框随时间轴移动的镜头，不得裁成瞬间出现。",
    ],
  }),
  defineAsset({
    assetId: "video-tracker-text-discovery",
    title: "文本发现双目标视频轨迹",
    theme: "用明确文本自动发现多个目标并建立跨帧轨迹",
    objective:
      "展示输入 bus 后由真实 SAM3 自动发现左右两辆公交车，逐目标检查候选、跨帧核对并采纳为两条新轨迹。",
    duration: { minSeconds: 12, targetSeconds: 18, maxSeconds: 90 },
    shots: [
      "在无选中源轨迹的画布级入口选择 SAM3 文本追踪、bus 类别与矩形框输出。",
      "输入明确目标文本 bus 并发起发现，展示真实模型返回的多目标候选池。",
      "按真实候选框识别左右两辆完整公交车，只保留这两个目标，取消远处目标和局部部件。",
      "逐帧前进到后段再回到中前段，核对两辆公交车的追踪框后采纳两条轨迹，并拒绝剩余噪声候选。",
    ],
    editingNotes: [
      "母版只表达纯文本无源发现，不混入点、框示例或已有轨迹传播；组合发现另录独立资产。",
      "必须保留 11 帧范围、输入文本、候选池筛选、两目标候选数量、逐帧核对、采纳双轨迹和拒绝剩余噪声的完整闭环。",
      "自动校验只接受与左右公交车锚点对齐的两条 bus 轨迹，中心 truck 不得被混入。",
    ],
  }),
  defineAsset({
    assetId: "video-tracker-combo-discovery",
    title: "SAM3 发现追踪组合链路",
    theme: "从文本发现到逐对象视频记忆追踪",
    objective:
      "展示 combo 模型先按 bus 文本发现目标，再以发现框为内部种子跨两个窗口保持身份，人工筛选并采纳左右两辆公交车。",
    duration: { minSeconds: 30, targetSeconds: 42, maxSeconds: 50 },
    shots: [
      "展示已配置好的 SAM3 发现追踪 combo、bus 类别、矩形框输出和 bus 文本，再开始发现。",
      "运行真实的文本发现 → 逐对象 PVS memory 两趟编排，展示 7 个身份覆盖 31 帧的 217 个候选。",
      "取消远处目标，只保留实例 2、4 对应的左右两辆完整公交车，共 62 个候选。",
      "将时间轴拖到第二窗口的后段再回到第一窗口，核对身份稳定后采纳两条轨迹并拒绝剩余候选。",
    ],
    editingNotes: [
      "combo 的点框种子由文本发现结果在内部铸造，当前产品不要求用户补画种子；母版不得伪造不存在的交互。",
      "保留模型下拉中的 combo 名称、31 帧范围、7 个稳定身份、跨窗口核对和最终双轨迹。",
      "可与纯文本发现母版并列剪辑，突出 combo 不会在第二窗口新增漂移身份。",
    ],
  }),
  defineAsset({
    assetId: "video-mask-correction-propagate",
    title: "视频 Mask 错帧纠正与重传播",
    theme: "用加减笔迹修正漂移边界并更新后续轨迹",
    objective:
      "展示已有卡车 Mask 在 F5 发生边界漂移后，用笔刷补入漏分区域、用橡皮扣除外溢区域，再以人工纠错帧为原生 Mask seed 向后续帧重传播并采纳更新。",
    duration: { minSeconds: 20, targetSeconds: 28, maxSeconds: 36 },
    shots: [
      "从已加载的 F5 错误边界开始，保留 F0 Mask 被保持到后续帧而产生漂移的证据。",
      "进入当前帧 Mask 编辑，先用笔刷补入卡车上沿漏分区域，再切换橡皮扣除右侧外溢区域。",
      "选择“更晚帧”并确认 SAM3 PVS 使用原生 Mask seed，从 F5 向后生成纠错候选。",
      "拖动时间轴到后段再回看中段，核对 Mask 候选随车辆运动后采纳，保存到原轨迹。",
    ],
    editingNotes: [
      "初始 Mask 轨迹在录制窗口前创建；母版只表达纠错，不混入从零创建 Mask。",
      "笔刷添加与橡皮扣除都必须完整可见，且路径基于 F5 卡车的已复核框，不在无关车辆或背景上瞎画。",
      "必须保留人工纠错帧、原生 Mask seed、向更晚帧、候选审阅、跨帧拖动和原轨迹更新的完整闭环。",
    ],
  }),
  defineAsset({
    assetId: "video-track-carryover",
    title: "跨帧虚影续写",
    theme: "使用上一帧虚影快速续写轨迹",
    objective: "展示切帧后根据虚影绘制新框，并用 Tab 续写到下一帧。",
    duration: { minSeconds: 9, targetSeconds: 13, maxSeconds: 28 },
    shots: ["展示当前轨迹框。", "切帧后显示上一帧虚影。", "续写新框并自动前进。"],
    editingNotes: ["虚影必须在画面中停留足够时间，便于观众识别。"],
  }),
  defineAsset({
    assetId: "video-mask-track-edit",
    title: "视频 Mask 轨迹编辑",
    theme: "创建 Mask 轨迹并在后续帧修正",
    objective: "展示首帧创建 Mask、切换到后续帧并编辑同一轨迹。",
    duration: { minSeconds: 10, targetSeconds: 16, maxSeconds: 35 },
    shots: ["在首帧创建 Mask 轨迹。", "切换到后续帧。", "修正 Mask 并展示轨迹延续。"],
    editingNotes: ["保留帧号和轨迹身份，避免被误解为两个独立 Mask。"],
  }),
  defineAsset({
    assetId: "ai-tracker-panel",
    title: "AI 追踪面板交互",
    theme: "可拖动、缩放的追踪任务面板",
    objective: "展示 AI 追踪面板的布局调整和交互互斥。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 25 },
    shots: ["打开 AI 追踪面板。", "拖动面板并调整大小。", "展示与画布操作的互斥关系。"],
    editingNotes: ["画面需包含面板边界和画布，便于理解拖动与缩放。"],
  }),
  defineAsset({
    assetId: "pointcloud-controls",
    title: "点云显示控件",
    theme: "相机上色、点大小与相机深度提示",
    objective: "展示通过控件改变点云的显示方式。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 40 },
    shots: [
      "展示初始点云与同步相机。",
      "切换相机上色方式并调整点大小。",
      "开启深度提示，展示相机图上的真实深度热力。",
    ],
    editingNotes: ["每次参数更改后保留稳定画面，便于做前后对比。"],
  }),
  defineAsset({
    assetId: "pointcloud-view",
    title: "点云视图导航",
    theme: "三维点云的旋转和视角检查",
    objective: "展示拖动旋转点云、观察三维结构并稳定落位。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 40 },
    shots: ["展示初始点云视角。", "平滑拖动旋转视图。", "停在新视角展示空间结构。"],
    editingNotes: ["避免高倍加速，三维旋转需保持方向感。"],
  }),
  defineAsset({
    assetId: "pointcloud-camera-seed-3d-box",
    title: "相机图辅助 3D 种框",
    theme: "从二维相机提示反投影到点云中的真实 3D 框",
    objective:
      "展示在经标定的相机图圈定目标，经视锥选点生成近似 3D 草案，再通过三视图人工精修并核对相机投影。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 36 },
    shots: [
      "展示同步相机图与点云场景。",
      "放大相机图并沿经复核的前景物体拖出二维提示框。",
      "确认真实 box_3d 标注落库并投影回相机图。",
      "关闭大图，在主点云与三视图人工修正草案尺寸，保留正式 PATCH 与读回的同一对象。",
      "旋转点云核对后重新放大相机图，展示 3D 框稳定重投影到原目标。",
    ],
    editingNotes: [
      "准确称为几何辅助草案与人工精修，不宣称 ML 推理或真值精度；保留提示、生成、修正和投影核对的完整因果链。",
    ],
  }),
  defineAsset({
    assetId: "pointcloud-crossframe-track",
    title: "3D 目标跨帧延续与修正",
    theme: "保留同一轨迹身份的确定性跨帧延续",
    objective:
      "展示已复核 3D 框延续到后续两帧，在中间帧修正位置，并通过邻帧参考框核对同一 track_id 链。",
    duration: { minSeconds: 22, targetSeconds: 30, maxSeconds: 40 },
    shots: [
      "从首帧已复核的 object 3D 框开始，保留完整点云与选中态。",
      "延续到第 1 帧，展示 nuScenes ego 位姿补偿、新框自动选中与同一轨迹编号。",
      "在第 1 帧通过数值面板修正框中心位置并保存。",
      "将修正后的框继续延续到第 2 帧，核对 track_id 未变。",
      "从第 2 帧回到第 1 帧，同时展示当前实线框与同轨迹前后两侧的邻帧虚线参考框。",
    ],
    editingNotes: [
      "该能力不是 AI 追踪；使用 nuScenes-mini scene-0061 的真实 ego 位姿与连续帧。",
      "源 3D 框在录制窗口前通过正式 API 准备，母版不混入相机种框或从零画框。",
      "ego 对齐后邻帧参考可以与当前框重合；用真实视角检查和轨迹读回核对，不移动框体伪造分离效果。",
    ],
  }),
  defineAsset({
    assetId: "pointcloud-billboard-label",
    title: "3D 框 Billboard 标签",
    theme: "配置点云标签内容并在多角度保持文字正对相机",
    objective:
      "展示为同一个已复核 3D 框启用属性标签，确认类别与属性进入 billboard 后，环绕点云从多个角度持续核对文字可读性。",
    duration: { minSeconds: 14, targetSeconds: 20, maxSeconds: 26 },
    shots: [
      "从已加载的点云和单个前景 3D 框开始，先展示仅含类别名的顶部标签。",
      "打开工作台设置的标签内容，切到轨迹段并启用属性。",
      "关闭设置后核对标签已显示 object、车辆与清晰可见三个信息片段。",
      "拉近目标并连续环绕多个方位，展示标签始终正对相机且文字不随框平面倾斜。",
      "停在最终斜视角，同时保留完整 3D 框、点云空间结构与可读标签。",
    ],
    editingNotes: [
      "该母版只表达 billboard 标签配置与视角适配，不混入种框、跨帧延续、相机投影或几何编辑。",
      "属性开关必须真正触发 CanvasTexture 绘制完整文本，不能只录设置窗口点击动作。",
      "源 3D 框在录制前通过正式 API 准备，结束后按 annotation ID 精确清理。",
    ],
  }),
  defineAsset({
    assetId: "pointcloud-panel-layout",
    title: "点云三视图与相机面板布局",
    theme: "3D 面板整体停靠、浮动、隐藏恢复与跨帧保持",
    objective:
      "展示选中 nuScenes 3D 框后，将三视图作为一个面板停靠 / 浮动 / 隐藏恢复，再把六路相机整组切换为悬浮与停靠图库，最后切到相邻帧核对渲染与相机角色仍然稳定。",
    duration: { minSeconds: 20, targetSeconds: 30, maxSeconds: 48 },
    shots: [
      "从选中的真实 3D 框开始，在框体精修布局展示主视图与俯、侧、正三视图同步。",
      "将三视图整体停靠到左侧、浮动，再隐藏并从布局菜单恢复；保留同一个选中框和渲染画布。",
      "切到传感器融合，展示 CAM_FRONT 等六路相机按物理朝向悬浮，并收入相机视图停靠图库。",
      "隐藏相机图库后恢复，切到 Scene 相邻帧，确认相机图、点云 renderer 和角色顺序仍完整。",
    ],
    editingNotes: [
      "相机只能整组在悬浮和停靠之间切换；三视图始终作为一个 Dockview 面板操作，不把独立相机窗口伪装成第三种停靠模式。",
      "所有底图、选择、相机角色和切帧都来自真实 nuScenes-mini 数据；该流程不执行 ML 推理，声明 none。",
      "结束后按正式 API 创建的单个 annotation ID 精确清理，失败时保留录制诊断。",
    ],
  }),
  defineAsset({
    assetId: "storage-connector-create-test",
    title: "S3 / OSS 连接器创建与测试",
    theme: "安全保存外部存储凭据并真实探测素材目录",
    objective: "展示创建一个脱敏 S3 / OSS 数据源，确认密钥已加密且正式连接测试返回有意义的样本数。",
    duration: { minSeconds: 14, targetSeconds: 20, maxSeconds: 28 },
    shots: [
      "从数据集页切换到数据连接器，确认初始列表已加载。",
      "打开新建数据源对话框，填写演示 Endpoint、Bucket、前缀和凭据。",
      "关闭 HTTPS 以匹配本地演示存储，提交并确认列表显示密钥已加密。",
      "对新建行发起真实连接测试，经历测试中状态后显示连接成功。",
      "停留在绿色成功结果，同时保留样本 4、S3 类型、个人范围和已加密状态。",
    ],
    editingNotes: [
      "母版只表达连接器创建与测试，不混入数据集导入向导或主机白名单管理。",
      "凭据必须是隔离 MinIO 的演示值；密钥不得出现在 API 响应或录制文档中。",
      "连接成功和样本数必须来自正式测试端点；流程结束后按连接器 ID 精确清理。",
    ],
  }),
  defineAsset({
    assetId: "video-draw",
    title: "视频轨迹画框",
    theme: "关键帧标注与中间帧插值",
    objective: "展示在两个关键帧上画框，并查看中间帧的轨迹插值。",
    duration: { minSeconds: 11, targetSeconds: 17, maxSeconds: 35 },
    shots: ["在起始帧创建轨迹框。", "切换到后续帧并调整框。", "来回逐帧检查插值轨迹。"],
    editingNotes: ["保留至少一次完整的起始帧→中间帧→结束帧导航。"],
  }),
  defineAsset({
    assetId: "large-image-progressive",
    title: "大图渐进式高清加载",
    theme: "超大图像从概览到局部高清切片",
    objective: "展示缩放大图时级别切换、加载进度和高清细节到位。",
    duration: { minSeconds: 10, targetSeconds: 15, maxSeconds: 40 },
    shots: ["展示大图全景。", "快速缩放到局部并展示渐进加载。", "停留展示高清切片细节。"],
    editingNotes: ["不要删掉清晰度逐步提升的过程，它是该能力的主体。"],
  }),
  defineAsset({
    assetId: "large-image-pyramid-recovery",
    title: "大图高清切片自动恢复",
    theme: "单个高清切片请求失败后的无损降级与自动重试",
    objective: "展示局部高清切片短暂失败时保留概览、自动重试，并在恢复后继续流畅缩放平移。",
    duration: { minSeconds: 16, targetSeconds: 22, maxSeconds: 28 },
    shots: [
      "从完整大图进入局部高清视图。",
      "让一个真实切片请求返回 503，并展示自动重试状态与 overview 兜底。",
      "等待同一切片重新签名并恢复清晰。",
      "继续缩放和平移，确认相邻视口维持高清细节。",
    ],
    editingNotes: [
      "保留失败提示出现、自动消失与恢复后继续浏览的完整因果链；不得把故障画面伪造成产品结果。",
    ],
  }),
  defineAsset({
    assetId: "large-image-mask-limit",
    title: "超大图 Mask 能力边界",
    theme: "大图仍可浏览和矢量标注，Mask 超限原因明确可查",
    objective:
      "展示 Cosmic Cliffs 超大图的分块浏览和矩形标注不受影响，同时在 Mask 工具入口准确告知 14575×8441 图像超过单边与总像素上限。",
    duration: { minSeconds: 10, targetSeconds: 14, maxSeconds: 24 },
    shots: [
      "从已加载的 Cosmic Cliffs 全景开始，放大到清晰局部。",
      "选择矩形工具，在明确的星云结构上完整绘制并落库一个“目标”矢量框。",
      "悬停置灰的 Mask 工具，完整展示实际尺寸、单边上限和总像素上限。",
    ],
    editingNotes: [
      "该母版只表达超大图上的能力边界；不混入切片故障恢复或 AI 推理。",
      "矢量框使用正式标注接口落库，录制窗口结束后按精确 annotation id 清理。",
      "Mask 必须保持置灰，Tooltip 必须完整可读，不得把尺寸超限伪装成模型或权限错误。",
    ],
  }),
  defineAsset({
    assetId: "hotkey-cheatsheet",
    title: "快捷键速查面板",
    theme: "打开、搜索和关闭工作台快捷键帮助",
    objective: "展示用问号键打开快捷键面板，筛选命令并返回工作台。",
    duration: { minSeconds: 8, targetSeconds: 12, maxSeconds: 25 },
    shots: ["在工作台按问号键。", "展示分组快捷键列表并搜索。", "清空搜索或关闭面板。"],
    editingNotes: ["文字是主体，后期不要缩放到无法阅读。"],
  }),
  defineAsset({
    assetId: "video-issue-resolution",
    title: "视频问题定位与连续处理",
    theme: "从另一源帧阅读问题，定位到真实目标帧后回复并解决",
    objective:
      "展示在另一源帧打开已存在的问题详情，阅读不改变当前帧；点击「定位」等待真实目标帧与目标对象渲染完成后回复并解决，再继续阅读已解决详情并打开下一条未解决问题。",
    duration: { minSeconds: 22, targetSeconds: 32, maxSeconds: 46 },
    shots: [
      "在另一源帧打开已存在的问题详情，展示阅读不会改变当前帧。",
      "点击「定位」，等待真实目标帧渲染完成，并核对画布上的目标对象与问题图钉。",
      "回复并点击「解决」，保留真实的状态更新结果。",
      "继续阅读已解决详情，再用「下一条」打开下一条未解决问题。",
    ],
    editingNotes: [
      "定位必须等待真实目标帧渲染：ready 状态、已绘制帧、媒体像素真实变化与锚点图钉像素共同证明，不得只凭帧号变化判断。",
      "两条问题由正式接口在录制窗口前创建，流程结束后通过正式删除接口精确清理；不把阅读动作伪装成帧跳转，也不混入问题创建表单或候选审阅。",
      "目标帧与对象来自 catalog 已复核的车辆锚点；成片不得用局部部件或无关帧替代目标对象。",
    ],
  }),
  defineAsset({
    assetId: "project-guide-autosave",
    title: "标注指引表格编辑自动保存",
    theme: "不点击保存也能落库的指引编辑与刷新持久化",
    objective:
      "展示在可视化编辑器中直接改写标注指引的边界规则表格，观察真实的等待自动保存、保存中与已保存状态，再打开预览并刷新页面确认规则已由真实读取持久化。",
    duration: { minSeconds: 16, targetSeconds: 24, maxSeconds: 36 },
    shots: [
      "打开已有短指引的可视化编辑器，停在边界规则表格上。",
      "直接改写表格里的边界规则文字，全程不点击「保存」。",
      "观察真实的「等待自动保存 → 保存中… → 已保存」状态迁移。",
      "打开预览并刷新页面，展示规则已由真实读取持久化。",
    ],
    editingNotes: [
      "保存状态来自真实自动保存 PATCH/GET；成片不得出现点击「保存」按钮或伪造的保存成功。",
      "录制只恢复本次拥有项目的原始指引，不改动其他项目或账号；成片也不表示未保存草稿会跨刷新保留。",
      "该母版只表达表格规则编辑与自动保存，不混入图片上传、模板插入或源码模式。",
    ],
  }),
  defineAsset({
    assetId: "project-invitation-join",
    title: "项目邀请创建与被邀请人加入",
    theme: "从邀请记录到新账号真实注册并加入项目",
    objective:
      "展示待接受邀请记录，由未注册的被邀请人通过真实 UI 创建账号并接受邀请，再以真实项目成员关系和「等待分派」空态收尾；不包含邮件投递步骤。",
    duration: { minSeconds: 22, targetSeconds: 32, maxSeconds: 46 },
    shots: [
      "在用户管理确认待接受邀请的邮箱、员工平台身份与目标项目；项目质检职责由正式邀请 DTO 核对。",
      "真实退出管理员会话，打开邀请注册链接。",
      "由新用户填写演示称呼与掩码密码，提交注册并接受邀请。",
      "展示真实项目成员结果与「等待分派」的诚实空态。",
    ],
    editingNotes: [
      "邀请创建面板一次性明文展示注册链接，因此创建动作在录制窗口外完成；成片不含任何邀请 token 字段，也不包含邮件投递步骤。",
      "该片段覆盖新账号注册，不声称拍摄已有账号登录确认路径；加入项目不会自动产生激活批次，员工平台身份不等同于项目质检职责。",
      "只使用隔离演示身份；录制结束后撤销本流程拥有的邀请，已接受的邀请精确移除本次新增的项目成员关系。",
    ],
  }),
] satisfies MarketingAssetSpec[];

export const MARKETING_ASSET_SPECS: ReadonlyMap<string, MarketingAssetSpec> = new Map(
  assetSpecs.map((spec) => [spec.assetId, spec]),
);
if (MARKETING_ASSET_SPECS.size !== assetSpecs.length) {
  throw new Error("[marketing] 营销资产规格中存在重复 asset id");
}

export function marketingAssetSpec(assetId: string): MarketingAssetSpec {
  const spec = MARKETING_ASSET_SPECS.get(assetId);
  if (!spec) {
    throw new Error(`[marketing] 未登记的营销资产: ${assetId}`);
  }
  return spec;
}
