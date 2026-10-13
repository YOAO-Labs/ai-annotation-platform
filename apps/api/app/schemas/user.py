from typing import Annotated, Any, Literal
from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from uuid import UUID
from datetime import datetime

from app.schemas.workbench_workspace import WorkbenchWorkspacePreferences

NamedPresetsRevision = Annotated[
    str,
    Field(strict=True, pattern=r"^(?:0|[0-9a-f]{32})$"),
]


_SHORTCUT_MODIFIER_KEY_NAMES = frozenset(
    {"shift", "control", "alt", "altgraph", "meta", "capslock", "numlock", "fn"}
)


class ShortcutBinding(BaseModel):
    """v0.24 · 单条结构化快捷键绑定。

    ``key`` 是规范化的 ``KeyboardEvent.key``：单个字符（任意字符，如 ``/``）或
    小写命名键（如 ``arrowright``）。``modifiers`` 是显式修饰键集合，匹配时要求
    完全一致（``mod`` = 平台主修饰键，Ctrl/⌘）。
    """

    model_config = {"extra": "forbid"}

    key: str = Field(
        min_length=1, max_length=32, pattern=r"^(?:.{1}|[a-z][a-z0-9]{0,31})$"
    )
    modifiers: list[Literal["mod", "alt", "shift"]] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_not_modifier(cls, v: str) -> str:
        if v.lower() in _SHORTCUT_MODIFIER_KEY_NAMES:
            raise ValueError("key must not be a modifier key name")
        return v

    @field_validator("modifiers")
    @classmethod
    def _dedupe_sort(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for item in v:
            if item not in seen:
                seen.append(item)
        return sorted(seen)


# 可编辑命令的稳定 ID 及其所属域；与前端 state/hotkeyBindings.ts 的注册表一一对应。
# 本增量之外的新命令需先在前端注册再在此处放行（写路径按「域 + 命令 ID」严格校验）。
_SHORTCUT_COMMAND_DOMAINS: dict[str, str] = {
    "common.task.next": "common",
    "common.task.prev": "common",
    "image.tool.select": "image",
    "image.tool.box": "image",
    "image.tool.rotatedBox": "image",
    "image.tool.polygon": "image",
    "image.tool.polyline": "image",
    "image.tool.keypoint": "image",
    "image.tool.mask": "image",
    "image.tool.aiCycle": "image",
    "image.tool.magicBox": "image",
    "image.selection.lock": "image",
    "image.selection.hide": "image",
    "video.tool.select": "video",
    "video.tool.box": "video",
    "video.tool.rotatedBox": "video",
    "video.tool.keypoint": "video",
    "video.tool.track": "video",
    "video.tool.mask": "video",
    "video.tool.smartPoint": "video",
    "video.tool.smartBox": "video",
    "video.tool.exemplar": "video",
    "video.tool.magicBox": "video",
    "video.tool.polygon": "video",
    "video.track.locked": "video",
    "video.track.hidden": "video",
    "video.track.outside": "video",
    "video.track.occluded": "video",
    "video.track.bookmark": "video",
    "video.frame.next": "video",
    "video.frame.prev": "video",
    "video.frame.micro.next": "video",
    "video.frame.micro.prev": "video",
    "video.track.keyframe.next": "video",
    "video.track.keyframe.prev": "video",
}


def shortcut_command_domain(command_id: str) -> str | None:
    """返回命令所属的覆盖域（common / image / video）；未注册命令返回 None。"""
    return _SHORTCUT_COMMAND_DOMAINS.get(command_id)


_ShortcutOverrideBucket = dict[str, list[ShortcutBinding] | None]


class WorkbenchShortcutPreferences(BaseModel):
    """v0.24 · 账号级快捷键覆盖。

    每个桶（common / image / video）将命令 ID 映射到该命令的完整组合列表：
    缺失或 ``null`` = 用默认组合；空列表 = 停用；非空列表（≤2 条，一主一备）=
    整体替换默认组合。PATCH 只提交变更的命令条目（深合并按命令生效）。
    """

    model_config = {"extra": "forbid"}

    # 只接受当前受支持的 schema 版本；更高版本由客户端保留为 opaque 子树，
    # 写路径不能用一个「看起来合法」的版本来静默停用全部覆盖。
    schemaVersion: Literal[1] = 1
    common: _ShortcutOverrideBucket = Field(default_factory=dict)
    image: _ShortcutOverrideBucket = Field(default_factory=dict)
    video: _ShortcutOverrideBucket = Field(default_factory=dict)

    @field_validator("common", "image", "video")
    @classmethod
    def _validate_bucket(
        cls, v: _ShortcutOverrideBucket, info: ValidationInfo
    ) -> _ShortcutOverrideBucket:
        domain = info.field_name
        for command_id, value in v.items():
            if value is None:
                # 显式恢复默认（重置）对未知 ID 也放行：客户端据此清除损坏存量条目。
                continue
            expected = shortcut_command_domain(command_id)
            if expected is None:
                raise ValueError(f"unknown shortcut command id: {command_id}")
            if expected != domain:
                # 命令 ID 必须落在自己的域桶；否则写入会通过校验却被生效表忽略。
                raise ValueError(
                    f"shortcut command {command_id} does not belong to the "
                    f"{domain} bucket"
                )
            if len(value) > 2:
                raise ValueError(
                    f"shortcut command {command_id} allows at most 2 bindings"
                )
        return v


class FloatingPanelState(BaseModel):
    """v0.13.10 · 工作台浮窗状态。像素默认由前端按窗口计算。"""

    model_config = {"extra": "forbid"}

    detached: bool = False
    x: int | None = None
    y: int | None = None
    w: int | None = Field(default=None, ge=48, le=720)
    h: int | None = Field(default=None, ge=120, le=900)


class TriViewFloatState(BaseModel):
    """v0.13.10 · 3D 三视图浮层位置、尺寸与折叠态。"""

    model_config = {"extra": "forbid"}

    collapsed: bool = False
    x: int | None = None
    y: int | None = None
    w: int | None = Field(default=None, ge=200, le=480)
    h: int | None = Field(default=None, ge=240, le=720)


class FloatingSelectionState(BaseModel):
    """v0.16.8 · 选中标注浮动信息卡的位置、尺寸与折叠态(跨设备)。

    与边栏浮窗不同,选中卡无「合并回边栏」语义,故只有 collapsed(无 detached);
    显隐由选中状态驱动,这里只持久化几何与折叠。尺寸界与 FloatingPanelState 一致。
    """

    model_config = {"extra": "forbid"}

    collapsed: bool = False
    x: int | None = None
    y: int | None = None
    w: int | None = Field(default=None, ge=48, le=720)
    h: int | None = Field(default=None, ge=120, le=900)


class CameraPanelState(BaseModel):
    """v0.15.x · 3D 悬浮相机面板的位置 + 折叠态。按相机 role 分桶存。

    位置像素由前端按视图计算后落库;无该 role 键 = 用默认贴边位置 + 自动折叠态。
    """

    model_config = {"extra": "forbid"}

    x: float | None = None
    y: float | None = None
    collapsed: bool = False


class PointcloudCameraState(BaseModel):
    """v0.15.x · 点云主视角快照。由前端 OrbitControls 写入/恢复。"""

    model_config = {"extra": "forbid"}

    position: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float]
    mode: Literal["orbit", "bev"] = "orbit"


class WorkbenchLayoutPreferences(BaseModel):
    """v0.13.10 · 工作台布局偏好。

    后端只校验结构；默认值和 PATCH 全量 workbench 契约由前端维护。
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    left_open: bool | None = Field(default=None, alias="leftOpen")
    right_open: bool | None = Field(default=None, alias="rightOpen")
    # v0.20.19 · 右栏「标注详情」属性区折叠态 (随账号持久, 不再每次选框重置)。
    attr_panel_collapsed: bool | None = Field(default=None, alias="attrPanelCollapsed")
    # v0.20.22 · 右栏「AI 待审」/「人工」分组头折叠 + 右栏下段「讨论」完全收起 (跨设备持久)。
    # 强类型 extra="forbid" → 未在此声明的字段前端 PATCH 会 422 直接落不下去,
    # 前端也就永远拿到默认值 (刷新后又全部展开)。
    ai_section_collapsed: bool | None = Field(default=None, alias="aiSectionCollapsed")
    manual_section_collapsed: bool | None = Field(
        default=None, alias="manualSectionCollapsed"
    )
    # 右栏「轨迹」分组头折叠 (视频任务; 跨设备持久)。
    track_section_collapsed: bool | None = Field(
        default=None, alias="trackSectionCollapsed"
    )
    discussion_collapsed: bool | None = Field(default=None, alias="discussionCollapsed")
    floating_task_queue: FloatingPanelState | None = Field(
        default=None,
        alias="floatingTaskQueue",
    )
    floating_class_palette: FloatingPanelState | None = Field(
        default=None,
        alias="floatingClassPalette",
    )
    floating_inspector: FloatingPanelState | None = Field(
        default=None,
        alias="floatingInspector",
    )
    floating_discussion: FloatingPanelState | None = Field(
        default=None,
        alias="floatingDiscussion",
    )
    floating_selection: FloatingSelectionState | None = Field(
        default=None,
        alias="floatingSelection",
    )
    tri_view_float: TriViewFloatState | None = Field(default=None, alias="triViewFloat")
    camera_panels: dict[str, CameraPanelState] = Field(
        default_factory=dict, alias="cameraPanels"
    )
    pointcloud_camera: PointcloudCameraState | None = Field(
        default=None, alias="pointcloudCamera"
    )
    workspace: WorkbenchWorkspacePreferences | None = None

    @field_validator("workspace", mode="before")
    @classmethod
    def _workspace_cannot_be_cleared(cls, value):
        if value is None:
            raise ValueError("workspace cannot be cleared; replace the current context")
        return value


class LabelContentByType(BaseModel):
    """v0.16.7 · 标签内容按标注类型分段；class 三段恒显，不入表。

    single=图片手工框 / track=视频轨迹框 / ai=图片预测框，各段只列该类型有意义的字段。
    旧扁平 list 由 WorkbenchCommonPreferences 的 before validator 迁移为本结构。
    """

    model_config = {"extra": "forbid"}

    # 分组号 #id / 属性。
    single: list[Literal["id", "attrs"]] = Field(default_factory=list)
    # 轨迹号 #num / 状态(插值·遮挡) / 属性。
    track: list[Literal["id", "state", "attrs"]] = Field(
        default_factory=lambda: ["id", "state"]
    )
    # ✦来源前缀 / 置信度 / 分组号 / 属性。
    ai: list[Literal["source", "score", "id", "attrs"]] = Field(
        default_factory=lambda: ["source", "score"]
    )

    @field_validator("single", "track", "ai")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        # 去重保序，避免重复 token。
        seen: list[str] = []
        for item in v:
            if item not in seen:
                seen.append(item)
        return seen


class WorkbenchCommonPreferences(BaseModel):
    """v0.15.3 · 跨模态通用偏好（性能观测等）。"""

    model_config = {"extra": "forbid"}

    longTaskSampleRate: float = Field(default=0.05, ge=0.0, le=1.0)
    confirmDelete: Literal["never", "multi_only", "always"] = "never"
    recentClassesLimit: int = Field(default=5, ge=3, le=20)
    # v0.15.19 · 邻帧框叠加独立开关。历史偏好没有该字段时,由 crossFrameOverlayK>0 推导。
    crossFrameOverlayEnabled: bool = False
    # v0.15.6 · 邻帧框叠加帧数档位。0 仅兼容历史关闭值;新 UI 用独立开关表达关闭。
    crossFrameOverlayK: Literal[0, 1, 3, 5, 7] = 1
    # v0.15.17 · 邻帧框叠加范围:selected=仅选中对象 group(现状);all=不选对象也叠全部邻帧框。
    crossFrameOverlayScope: Literal["selected", "all"] = "selected"
    performanceTier: Literal["light", "standard", "aggressive"] = "standard"
    # 边栏宽度按工作台宽度的百分比存（替代旧 layout.leftWidth/rightWidth 像素值）。
    leftWidthPct: float = Field(default=15.0, ge=10, le=35)
    rightWidthPct: float = Field(default=15.0, ge=10, le=35)
    # v0.15.27 · 标注视觉样式（图片 + 视频共享，单一来源 annotationVisual.ts 消费）。
    # 标签字号（画布缩放跟随由前端按模态处理，这里只存基准 px）。
    labelFontSize: int = Field(default=12, ge=8, le=24)
    # 标签显隐：always=恒显 / selected=仅选中时 / none=不显示（取代旧 image.showBoxLabels）。
    labelVisibility: Literal["always", "selected", "none"] = "always"
    # v0.16.7 · 标签内容按标注类型分段（single/track/ai）；旧扁平 list 由 before validator 迁移。
    labelContent: LabelContentByType = Field(default_factory=LabelContentByType)
    # 描边线宽（screen px 基准；选中态 = 基值 + 0.5）。
    strokeWidth: float = Field(default=1.5, ge=1, le=5)
    # 闭合形状填充透明度（非选中）。
    fillOpacity: float = Field(default=0.07, ge=0, le=0.6)
    # 选中对象填充加重透明度。
    fillOpacitySelected: float = Field(default=0.12, ge=0, le=0.8)
    # v0.20.x · 工作台桌宠（常驻像素小精灵）开关；历史偏好缺该字段时默认开启。
    petEnabled: bool = True
    # v0.21.11 · 选中自动聚焦：键盘循环 / 点选对象时若出视口或过小则自动平移居中 + 适度放大。
    # 视频 + 图片 2D 工作台通用；默认关（不改选中前不移动视口的现状）。
    focusSelectionEnabled: bool = False
    # v0.21.11 · 采纳/拒绝 AI 候选后自动前进到下一个待决（仅移动选中，不缩放视口）。
    # 视频 + 图片 2D 通用；默认开（审阅流水线手感）。历史偏好缺该字段时默认开启。
    autoAdvanceOnDecide: bool = True
    # Image-canvas comment badges; missing historic values default to enabled.
    showAnnotationComments: bool = True

    @field_validator("labelContent", mode="before")
    @classmethod
    def _migrate_label_content(cls, v: Any) -> Any:
        # v0.16.7 · 旧扁平 list（["class","score"] 等）迁移为按类型分段对象。
        # 旧值只作用过图片：single/ai 按老勾选分发（ai 补 source 保留 ✦ 前缀），
        # track 用默认 ["id","state"]（= 旧视频硬编码观感）。class 恒显，丢弃。
        if isinstance(v, list):
            old = [t for t in v if isinstance(t, str)]
            single = [t for t in ("id", "attrs") if t in old]
            ai = ["source", *[t for t in ("score", "id", "attrs") if t in old]]
            return {"single": single, "track": ["id", "state"], "ai": ai}
        return v

    @model_validator(mode="after")
    def _derive_legacy_overlay_enabled(self):
        if (
            "crossFrameOverlayEnabled" not in self.model_fields_set
            and "crossFrameOverlayK" in self.model_fields_set
            and self.crossFrameOverlayK > 0
        ):
            self.crossFrameOverlayEnabled = True
        return self


class WorkbenchImagePreferences(BaseModel):
    """v0.15.3 · 图像模态偏好（2D 画布渲染 / 顶点手柄）。"""

    model_config = {"extra": "forbid"}

    smoothImage: bool = True
    cssImageFilter: str = Field(default="", max_length=255)
    controlPointsSize: int = Field(default=6, ge=2, le=20)
    autoFitOnResize: bool = True
    snapToGrid: bool = False
    afterBoxCreate: Literal["pick_class", "reuse_active"] = "pick_class"
    snapThresholdPx: int = Field(default=8, ge=4, le=16)
    zoomStepFactor: Literal[1.05, 1.1, 1.15, 1.2] = 1.1
    fadedOpacity: float = Field(default=0.35, ge=0.1, le=0.8)
    # v0.15.27 · showBoxLabels 迁移到 common.labelVisibility（三态枚举），此处删除。
    maskOverlayOpacity: float = Field(default=0.45, ge=0.2, le=0.8)


class WorkbenchVideoPreferences(BaseModel):
    """v0.15.5 · 视频模态偏好（播放 / 步进）。"""

    model_config = {"extra": "forbid"}

    defaultPlaybackRate: Literal[0.25, 0.5, 1, 2, 4] = 1
    largeFrameStep: Literal[5, 10, 30, "grid"] = 10
    autoFitOnResize: bool = True
    # v0.21.12 · 轨迹「续写后自动前进」：跨网格帧续写完一条轨迹后，自动选中同帧下一条待续轨迹
    # （上一网格帧有关键帧、当前帧未画者）。默认关（保持逐条手动 Tab / 点选的现状）。
    trackContinueAutoAdvance: bool = False


class WorkbenchPointcloudPreferences(BaseModel):
    """v0.15.3 · 点云模态偏好。v0.15.6 填充渲染 / 导航字段（默认值 = 拆分前现状值）。"""

    model_config = {"extra": "forbid"}

    pointSize: float = Field(default=0.06, ge=0.01, le=0.3)
    persistCameraView: bool = False
    colorizeWithCamera: bool = False
    colorizeContrast: float = Field(default=1, ge=0.5, le=2.5)
    colorizeBrightness: float = Field(default=0, ge=-0.5, le=0.5)
    colorizeGamma: float = Field(default=1, ge=0.5, le=3)
    showDepthHint: bool = False
    pointMaskSelectMode: Literal["rect", "lasso", "polygon"] = "rect"
    showGrid: bool = True
    showAxisGizmo: bool = True
    # OrbitControls dampingFactor：值越小惯性越强（前端文案「相机灵敏度」）。
    cameraDamping: float = Field(default=0.1, ge=0.05, le=0.3)
    # v0.15.18 · 邻帧点云叠加(ego 补偿对齐前后帧点云)。需 ego 轨迹;默认关。
    neighborPointOverlay: bool = False
    # v0.15.19 · 邻帧点云叠加帧数,独立于邻帧框叠加;点云较重,限制为 1-3。
    neighborPointOverlayK: Literal[1, 2, 3] = 1
    # v0.15.22/v0.15.23 · 邻帧点云动态目标处理:保留拖影 / 剔除 / 逐目标对齐。
    neighborPointCull: Literal["keep", "cull", "align"] = "keep"


class WorkbenchPreferences(BaseModel):
    """v0.9.41 · 标注工作台渲染偏好（I17 Configuration）。
    v0.13.10 · 增加 layout 子树承载跨设备布局偏好。
    v0.15.3 · 平铺字段拆为 common/image/video/pointcloud 四子树；layout 保持顶层。
    v0.24 · 增加 shortcuts 子树承载账号级快捷键覆盖（写路径严格校验）。"""

    model_config = {"extra": "forbid"}

    common: WorkbenchCommonPreferences = Field(
        default_factory=WorkbenchCommonPreferences
    )
    image: WorkbenchImagePreferences = Field(default_factory=WorkbenchImagePreferences)
    video: WorkbenchVideoPreferences = Field(default_factory=WorkbenchVideoPreferences)
    pointcloud: WorkbenchPointcloudPreferences = Field(
        default_factory=WorkbenchPointcloudPreferences
    )
    layout: WorkbenchLayoutPreferences = Field(
        default_factory=WorkbenchLayoutPreferences
    )
    shortcuts: WorkbenchShortcutPreferences | None = None


class AIToolPreferences(BaseModel):
    """每用户的 AI 工具偏好，按 ML backend id 分桶。

    不同 backend 的 /setup.params schema 不同（gsam2 有 box/text_threshold，sam3 有
    score_threshold 等），故按 backend id 各存一份，互不污染；多用户各自一份 preferences，
    天然隔离不打架。值为 /setup.params 对应的自由 dict，平台只做存取不强校验字段。

    v0.18.25 · 新增 model_by_backend：交互工具的引擎(模型)选择，同样按 backend 分桶。
    本子树为「深一层合并」(update_preferences)，故 params/model 两个子键可各自独立保存。"""

    model_config = {"extra": "forbid"}

    params_by_backend: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_by_backend: dict[str, str] = Field(default_factory=dict)
    # v0.18.31 · 交互后端(引擎)选择, 按 project 分桶(每项目各记用哪个交互后端)。此前存
    # localStorage(wb:preferred-interactive)不跨设备 = BUG; 现迁服务端与 model/params 对齐
    # (深合并故三键各自独立保存)。注: 按 project 分桶, 与 model 选择按 backend 分桶是有意区别。
    interactive_backend_by_project: dict[str, str] = Field(default_factory=dict)
    # v0.20.17 · 单框二次推理的参数 + 模型变体偏好, 按 `backendId:modelId` 分桶(比 backend
    # 更细: 二次推理选具体 model, 同 backend 多 model 的档位/阈值不互相串)。每桶为自由 dict
    # {params: {...}, variants: {...}}, 平台只存取不强校验(随模型 schema 变化, 旧键无害忽略)。
    secondary_by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)


class OnboardingProjectState(BaseModel):
    """Per-user onboarding state for one project's current guide revision.

    This lives in the existing user preferences JSONB so guide dismissal and
    reading progress follows the employee across devices without introducing a
    second onboarding table.  ``guide_version`` is supplied by the client from
    the persisted project guide; a changed guide naturally starts a new state.
    """

    model_config = {"extra": "forbid"}

    guide_version: str = Field(min_length=1, max_length=128)
    dismissed: bool = False
    guide_read: bool = False


class OnboardingPreferences(BaseModel):
    """Cross-device state for the employee/project start checklist."""

    model_config = {"extra": "forbid"}

    projects: dict[str, OnboardingProjectState] = Field(default_factory=dict)


class UIPreferences(BaseModel):
    """v0.15.25 · 全局 UI 偏好（工作台之外）；跟随账号跨设备。

    主题原先只存 localStorage（仅本机），升级到服务端偏好后换设备登录即保持。
    'system' = 跟随操作系统 prefers-color-scheme。"""

    model_config = {"extra": "forbid"}

    theme: Literal["light", "dark", "system"] = "system"
    # v0.20.19 · 二次推理面板显隐 (跨设备): true=隐藏工具条。默认 false=显示 (不回归现状)。
    secondary_bar_hidden: bool = False
    # 登录后「版本更新提醒」已确认的版本。空串 = 从未确认；前端按语义化版本比较，
    # 仅当构建版本更新时弹一次「本次更新」，点「知道了」写回本键，跨设备去重。
    changelog_seen_version: str = ""


class UserPreferences(BaseModel):
    """User.preferences JSONB root. 仅声明已知子树；未来按 epic 追加。"""

    model_config = {"extra": "forbid"}

    workbench: WorkbenchPreferences = Field(default_factory=WorkbenchPreferences)
    ai: AIToolPreferences = Field(default_factory=AIToolPreferences)
    ui: UIPreferences = Field(default_factory=UIPreferences)
    onboarding: OnboardingPreferences = Field(default_factory=OnboardingPreferences)
    # Opaque compare-and-swap token for the atomic namedPresets map. "0" is the
    # virtual revision before the first guarded write; successful writes use UUID hex.
    named_presets_revision: NamedPresetsRevision = Field(
        default="0", alias="namedPresetsRevision"
    )


class WorkbenchLayoutPreferencesRead(WorkbenchLayoutPreferences):
    """Read shape with an opaque fallback for rolling workspace upgrades."""

    workspace: WorkbenchWorkspacePreferences | dict[str, Any] | None = None

    @field_validator("workspace", mode="before")
    @classmethod
    def _workspace_cannot_be_cleared(cls, value):
        """Override the write-only validator: stored null remains readable."""
        return value


class WorkbenchPreferencesRead(WorkbenchPreferences):
    """Read shape with opaque fallbacks for rolling upgrades.

    ``workspace`` keeps stored layout envelopes verbatim; ``shortcuts`` tolerates
    corrupt or newer shortcut subtrees so one bad bucket cannot fail the whole
    preference GET. The route strips and re-inserts both around validation.
    """

    layout: WorkbenchLayoutPreferencesRead = Field(
        default_factory=WorkbenchLayoutPreferencesRead
    )
    shortcuts: WorkbenchShortcutPreferences | dict[str, Any] | None = None


class UserPreferencesRead(UserPreferences):
    """Preference response; writes continue to validate against UserPreferences."""

    workbench: WorkbenchPreferencesRead = Field(
        default_factory=WorkbenchPreferencesRead
    )


class UserCreate(BaseModel):
    email: str
    name: str
    password: str
    #: Account/platform role.  Administrator-created ordinary staff default to
    #: ``employee``; public registration is always a viewer and does not use
    #: this schema.  Legacy global annotator/reviewer inputs are rejected.
    role: str = "employee"

    @field_validator("role")
    @classmethod
    def _validate_platform_role(cls, value: str) -> str:
        from app.db.enums import LEGACY_PLATFORM_STAFF_ROLES, PLATFORM_ROLES

        if value in LEGACY_PLATFORM_STAFF_ROLES:
            raise ValueError("旧全局角色已废弃，请使用 employee")
        if value not in PLATFORM_ROLES:
            raise ValueError(f"非法角色: {value}")
        return value


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str
    role: str
    group_name: str | None
    group_id: UUID | None = None
    status: str
    is_active: bool = True
    disabled_kind: str | None = None
    disabled_at: datetime | None = None
    disabled_by: UUID | None = None
    disabled_reason: str | None = None
    # v0.12.0 · 邮箱验证时间戳；None = 未验证。仅开放注册 + 验证开关打开时作登录 gate。
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    # v0.8.3 · 心跳机制：最近一次活跃时间（登录 / POST /me/heartbeat / 关键操作）
    last_seen_at: datetime | None = None
    # v0.8.1 · 非空 = 管理员刚重置密码，前端登录后跳「强制改密」页
    password_admin_reset_at: datetime | None = None
    # v0.8.1 · 自助注销冷静期信息（已申请时返回 scheduled_at）；未申请均为 None
    deactivation_requested_at: datetime | None = None
    deactivation_scheduled_at: datetime | None = None
    # v0.9.41 · 标注偏好（workbench 渲染配置等）。空对象 = 用客户端默认。
    preferences: dict = Field(default_factory=dict)
    # 头像引用（preset:<slug> / upload:<token>）；None = 首字母回退。
    avatar_ref: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserReceiverOption(BaseModel):
    """A role-specific, currently eligible offboarding receiver."""

    id: UUID
    name: str
    email: str
    role: str
    project_member_role: str | None = None


class OffboardingBlocker(BaseModel):
    code: str
    message: str


class OffboardingBatchRef(BaseModel):
    batch_id: UUID
    batch_name: str


class OffboardingRolePreview(BaseModel):
    present: bool = False
    batches: list[OffboardingBatchRef] = Field(default_factory=list)
    receiver_options: list[UserReceiverOption] = Field(default_factory=list)


class OffboardingProjectPreview(BaseModel):
    project_id: UUID
    project_name: str
    roles: dict[str, OffboardingRolePreview]
    tasks: dict[str, dict[str, int]]
    locked_task_count: int = 0
    blockers: list[OffboardingBlocker] = Field(default_factory=list)


class OffboardingApiKeyPreview(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class OffboardingPreview(BaseModel):
    user: UserOut
    preview_version: str
    generated_at: datetime
    projects: list[OffboardingProjectPreview] = Field(default_factory=list)
    api_keys: list[OffboardingApiKeyPreview] = Field(default_factory=list)
    blockers: list[OffboardingBlocker] = Field(default_factory=list)
    can_commit: bool = False


class OffboardingProjectRequest(BaseModel):
    project_id: UUID
    owner_receiver_id: UUID | None = None
    annotator_receiver_id: UUID | None = None
    reviewer_receiver_id: UUID | None = None


class OffboardingCommitRequest(BaseModel):
    preview_version: str
    reason: str = Field(default="", max_length=500)
    mode: Literal["handoff", "emergency_suspend"] = "handoff"
    projects: list[OffboardingProjectRequest] = Field(default_factory=list)


class OffboardingTransferResult(BaseModel):
    project_id: UUID
    role: str
    receiver_id: UUID | None = None
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingUnresolvedResult(BaseModel):
    project_id: UUID | None = None
    role: str | None = None
    reason: str
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingResult(BaseModel):
    user: UserOut
    status: str
    mode: Literal["handoff", "emergency_suspend"]
    transfers: list[OffboardingTransferResult] = Field(default_factory=list)
    unresolved: list[OffboardingUnresolvedResult] = Field(default_factory=list)
    revoked_api_key_ids: list[UUID] = Field(default_factory=list)
    audit_id: int | None = None


class ReactivateRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class UserBrief(BaseModel):
    """v0.7.2 · 责任人可视化用：列表 / 卡片侧 inline 渲染头像 + 名字 + 角色。"""

    id: UUID
    name: str
    email: str
    role: str | None = None
    avatar_initial: str
    # 头像引用（preset:<slug> / upload:<token>）；None = 前端回退 avatar_initial。
    avatar_ref: str | None = None

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: str
    password: str
    # v0.9.3 · progressive CAPTCHA：同 IP 失败 ≥ 阈值后必填；前端从 401 响应头
    # X-Login-Failed-Count 拿到当前 count，达阈值时渲染 <Captcha>。
    captcha_token: str | None = None
