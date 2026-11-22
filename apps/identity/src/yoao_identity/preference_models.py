from typing import Annotated, Any, Literal
from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .workbench_workspace import WorkbenchWorkspacePreferences

NamedPresetsRevision = Annotated[
    str,
    Field(strict=True, pattern=r"^(?:0|[0-9a-f]{32})$"),
]


_SHORTCUT_MODIFIER_KEY_NAMES = frozenset(
    {"shift", "control", "alt", "altgraph", "meta", "capslock", "numlock", "fn"}
)


class ShortcutBinding(BaseModel):
    """单条结构化快捷键绑定。

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
    """账号级快捷键覆盖。

    每个桶（common / image / video）将命令 ID 映射到该命令的完整组合列表：
    缺失或 ``null`` = 用默认组合；空列表 = 停用；非空列表（≤2 条，一主一备）=
    整体替换默认组合。PATCH 只提交变更的命令条目（深合并按命令生效）。
    """

    model_config = {"extra": "forbid"}

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
                continue
            expected = shortcut_command_domain(command_id)
            if expected is None:
                raise ValueError(f"unknown shortcut command id: {command_id}")
            if expected != domain:
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
    """工作台浮窗状态。像素默认由前端按窗口计算。"""

    model_config = {"extra": "forbid"}

    detached: bool = False
    x: int | None = None
    y: int | None = None
    w: int | None = Field(default=None, ge=48, le=720)
    h: int | None = Field(default=None, ge=120, le=900)


class TriViewFloatState(BaseModel):
    """3D 三视图浮层位置、尺寸与折叠态。"""

    model_config = {"extra": "forbid"}

    collapsed: bool = False
    x: int | None = None
    y: int | None = None
    w: int | None = Field(default=None, ge=200, le=480)
    h: int | None = Field(default=None, ge=240, le=720)


class FloatingSelectionState(BaseModel):
    """选中标注浮动信息卡的位置、尺寸与折叠态(跨设备)。

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
    """3D 悬浮相机面板的位置 + 折叠态。按相机 role 分桶存。

    位置像素由前端按视图计算后落库;无该 role 键 = 用默认贴边位置 + 自动折叠态。
    """

    model_config = {"extra": "forbid"}

    x: float | None = None
    y: float | None = None
    collapsed: bool = False


class PointcloudCameraState(BaseModel):
    """点云主视角快照。由前端 OrbitControls 写入/恢复。"""

    model_config = {"extra": "forbid"}

    position: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float]
    mode: Literal["orbit", "bev"] = "orbit"


class WorkbenchLayoutPreferences(BaseModel):
    """工作台布局偏好。

    后端只校验结构；默认值和 PATCH 全量 workbench 契约由前端维护。
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    left_open: bool | None = Field(default=None, alias="leftOpen")
    right_open: bool | None = Field(default=None, alias="rightOpen")
    attr_panel_collapsed: bool | None = Field(default=None, alias="attrPanelCollapsed")
    ai_section_collapsed: bool | None = Field(default=None, alias="aiSectionCollapsed")
    manual_section_collapsed: bool | None = Field(
        default=None, alias="manualSectionCollapsed"
    )
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
    """标签内容按标注类型分段；class 三段恒显，不入表。

    single=图片手工框 / track=视频轨迹框 / ai=图片预测框，各段只列该类型有意义的字段。
    旧扁平 list 由 WorkbenchCommonPreferences 的 before validator 迁移为本结构。
    """

    model_config = {"extra": "forbid"}

    single: list[Literal["id", "attrs"]] = Field(default_factory=list)
    track: list[Literal["id", "state", "attrs"]] = Field(
        default_factory=lambda: ["id", "state"]
    )
    ai: list[Literal["source", "score", "id", "attrs"]] = Field(
        default_factory=lambda: ["source", "score"]
    )

    @field_validator("single", "track", "ai")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for item in v:
            if item not in seen:
                seen.append(item)
        return seen


class WorkbenchCommonPreferences(BaseModel):
    """跨模态通用偏好（性能观测等）。"""

    model_config = {"extra": "forbid"}

    longTaskSampleRate: float = Field(default=0.05, ge=0.0, le=1.0)
    confirmDelete: Literal["never", "multi_only", "always"] = "never"
    recentClassesLimit: int = Field(default=5, ge=3, le=20)
    crossFrameOverlayEnabled: bool = False
    crossFrameOverlayK: Literal[0, 1, 3, 5, 7] = 1
    crossFrameOverlayScope: Literal["selected", "all"] = "selected"
    performanceTier: Literal["light", "standard", "aggressive"] = "standard"
    leftWidthPct: float = Field(default=15.0, ge=10, le=35)
    rightWidthPct: float = Field(default=15.0, ge=10, le=35)
    labelFontSize: int = Field(default=12, ge=8, le=24)
    labelVisibility: Literal["always", "selected", "none"] = "always"
    labelContent: LabelContentByType = Field(default_factory=LabelContentByType)
    strokeWidth: float = Field(default=1.5, ge=1, le=5)
    fillOpacity: float = Field(default=0.07, ge=0, le=0.6)
    fillOpacitySelected: float = Field(default=0.12, ge=0, le=0.8)
    petEnabled: bool = True
    focusSelectionEnabled: bool = False
    autoAdvanceOnDecide: bool = True
    showAnnotationComments: bool = True

    @field_validator("labelContent", mode="before")
    @classmethod
    def _migrate_label_content(cls, v: Any) -> Any:
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
    """图像模态偏好（2D 画布渲染 / 顶点手柄）。"""

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
    maskOverlayOpacity: float = Field(default=0.45, ge=0.2, le=0.8)


class WorkbenchVideoPreferences(BaseModel):
    """视频模态偏好（播放 / 步进）。"""

    model_config = {"extra": "forbid"}

    defaultPlaybackRate: Literal[0.25, 0.5, 1, 2, 4] = 1
    largeFrameStep: Literal[5, 10, 30, "grid"] = 10
    autoFitOnResize: bool = True
    trackContinueAutoAdvance: bool = False


class WorkbenchPointcloudPreferences(BaseModel):
    """点云模态偏好与渲染、导航字段。"""

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
    cameraDamping: float = Field(default=0.1, ge=0.05, le=0.3)
    neighborPointOverlay: bool = False
    neighborPointOverlayK: Literal[1, 2, 3] = 1
    neighborPointCull: Literal["keep", "cull", "align"] = "keep"


class WorkbenchPreferences(BaseModel):
    """标注工作台渲染偏好（I17 Configuration）。
    增加 layout 子树承载跨设备布局偏好。
    平铺字段拆为 common/image/video/pointcloud 四子树；layout 保持顶层。
    增加 shortcuts 子树承载账号级快捷键覆盖（写路径严格校验）。"""

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

    新增 model_by_backend：交互工具的引擎(模型)选择，同样按 backend 分桶。
    本子树为「深一层合并」(update_preferences)，故 params/model 两个子键可各自独立保存。"""

    model_config = {"extra": "forbid"}

    params_by_backend: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_by_backend: dict[str, str] = Field(default_factory=dict)
    interactive_backend_by_project: dict[str, str] = Field(default_factory=dict)
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
    """全局 UI 偏好（工作台之外）；跟随账号跨设备。

    主题原先只存 localStorage（仅本机），升级到服务端偏好后换设备登录即保持。
    'system' = 跟随操作系统 prefers-color-scheme。"""

    model_config = {"extra": "forbid"}

    theme: Literal["light", "dark", "system"] = "system"
    secondary_bar_hidden: bool = False
    changelog_seen_version: str = ""


class UserPreferences(BaseModel):
    """User.preferences JSONB root. 仅声明已知子树；未来按 epic 追加。"""

    model_config = {"extra": "forbid"}

    workbench: WorkbenchPreferences = Field(default_factory=WorkbenchPreferences)
    ai: AIToolPreferences = Field(default_factory=AIToolPreferences)
    ui: UIPreferences = Field(default_factory=UIPreferences)
    onboarding: OnboardingPreferences = Field(default_factory=OnboardingPreferences)
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
    preference GET. The portable policy restores both around strict validation.
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
