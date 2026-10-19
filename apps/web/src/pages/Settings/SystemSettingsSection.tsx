import { CopyableError } from "@/components/ui/CopyableError";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { clsx } from "clsx";

import {
  type SystemSettingKey,
  type SystemSettingMetadata,
  type SystemSettingsPatch,
  type SystemSettingsResponse,
} from "@/api/settings";
import { ApiError } from "@/api/client";
import {
  useResetSystemSettings,
  useSystemSettings,
  useTestSmtp,
  useUpdateSystemSettings,
} from "@/hooks/useSystemSettings";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";

type SettingGroup = "members" | "mail" | "imports" | "video";
type SettingInput = "boolean" | "integer" | "bytes" | "string";
type DraftValue = string | boolean;
type Drafts = Partial<Record<SystemSettingKey, DraftValue>>;
type SmtpPasswordMode = "preserve" | "change" | "clear";

interface SettingDefinition {
  key: SystemSettingKey;
  group: SettingGroup;
  label: string;
  description: string;
  fallbackDefault: DraftValue;
  input: SettingInput;
  fallbackUnit?: string;
  fallbackEffect: string;
}

interface SmtpDraft {
  host: string;
  port: string;
  user: string;
  from: string;
  passwordMode: SmtpPasswordMode;
  password: string;
}

interface SystemSettingsSectionProps {
  onDirtyChange?: (dirty: boolean) => void;
}

const FORM_CLASS = "flex flex-col gap-3.5 p-4";
const INPUT_CLASS =
  "box-border w-full appearance-none rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";
const BUTTON_CLASS =
  "inline-flex w-auto cursor-pointer items-center justify-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-60";
const PRIMARY_BUTTON_CLASS =
  "inline-flex w-auto cursor-pointer items-center justify-center gap-1.5 rounded-md border border-transparent bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60";
const GROUP_HEADER_CLASS =
  "flex items-start justify-between gap-3 border-b border-border px-4 py-3";
const FIELD_LABEL_CLASS = "mb-1 text-xs font-medium text-muted-foreground";
const BYTES_PER_GIB = 1_073_741_824n;
const MAX_SAFE_INTEGER_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);

const SETTING_DEFINITIONS: SettingDefinition[] = [
  {
    key: "allow_open_registration",
    group: "members",
    label: "开放注册",
    description: "允许新用户自助注册为 Viewer；关闭后只能通过邀请注册。",
    fallbackDefault: false,
    input: "boolean",
    fallbackEffect: "注册请求读取新的有效值；已登录会话不受影响。",
  },
  {
    key: "invitation_ttl_days",
    group: "members",
    label: "邀请有效期",
    description: "新建邀请链接的有效天数，已经创建的邀请不重新计算。",
    fallbackDefault: "7",
    input: "integer",
    fallbackUnit: "天",
    fallbackEffect: "只影响新创建的邀请。",
  },
  {
    key: "max_invitations_per_day",
    group: "members",
    label: "每位管理员邀请上限",
    description: "按邀请人分别计算的滚动 24 小时上限，不是自然日或全平台总量。",
    fallbackDefault: "30",
    input: "integer",
    fallbackUnit: "个 / 滚动 24 小时",
    fallbackEffect: "下一次创建邀请时读取，缓存传播最多约 30 秒。",
  },
  {
    key: "offline_threshold_minutes",
    group: "members",
    label: "用户离线判定时间",
    description: "只影响在线状态显示，不会强制退出用户。",
    fallbackDefault: "5",
    input: "integer",
    fallbackUnit: "分钟",
    fallbackEffect: "下一轮在线状态扫描采用新值，扫描调度仍保持原周期。",
  },
  {
    key: "frontend_base_url",
    group: "mail",
    label: "前端基础地址",
    description: "用于生成邀请和重置密码邮件中的链接。",
    fallbackDefault: "http://localhost:5173",
    input: "string",
    fallbackEffect: "只影响之后生成的链接。",
  },
  {
    key: "dataset_import_max_files",
    group: "imports",
    label: "单次连接器导入文件数",
    description: "一次导入枚举的文件数量预算，受理时形成快照。",
    fallbackDefault: "50000",
    input: "integer",
    fallbackUnit: "个文件",
    fallbackEffect: "后续新导入在 API 受理时读取；排队任务保留自己的快照。",
  },
  {
    key: "dataset_import_max_total_bytes",
    group: "imports",
    label: "单次连接器导入总量",
    description: "连接器枚举预算；不会限制所有上传，也不会宣称限制实际网络字节。",
    fallbackDefault: "200",
    input: "bytes",
    fallbackUnit: "GiB",
    fallbackEffect: "与文件数一起在受理时读取并写入导入任务快照。",
  },
  {
    key: "task_create_sync_threshold",
    group: "imports",
    label: "转后台建任务的数量阈值",
    description: "非空数据集的关联数量超过阈值时转为后台任务；0 表示全部异步。",
    fallbackDefault: "2000",
    input: "integer",
    fallbackUnit: "条数据",
    fallbackEffect: "下一次关联请求决定分支，不改变已经排队的任务。",
  },
  {
    key: "video_chunk_warmup_lookahead",
    group: "video",
    label: "视频向后预热块数",
    description: "命中当前视频块后额外预解码的后续块数量；0 关闭额外预热。",
    fallbackDefault: "1",
    input: "integer",
    fallbackUnit: "个块",
    fallbackEffect: "只影响新的视频请求，不取消已排队的预热。",
  },
];

const GROUPS: Array<{ key: SettingGroup; label: string; description: string }> = [
  { key: "members", label: "成员与邀请", description: "注册、邀请和在线状态的业务阈值。" },
  { key: "mail", label: "邮件与访问地址", description: "链接生成地址和 SMTP 发送配置。" },
  { key: "imports", label: "数据导入", description: "连接器导入预算与建任务分支。" },
  { key: "video", label: "视频体验", description: "视频请求的邻块预热范围。" },
];

const DEFAULT_METADATA: Record<SystemSettingKey, SystemSettingMetadata> = {
  allow_open_registration: {
    source: "deployment",
    deployment_default: false,
    updated_at: null,
    updated_by: null,
    value_type: "bool",
    unit: null,
    effect: "注册请求读取新的有效值；已登录会话不受影响。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
  invitation_ttl_days: {
    source: "deployment",
    deployment_default: 7,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "天",
    effect: "只影响新创建的邀请。",
    min_value: 1,
    max_value: 90,
    in_range: true,
  },
  max_invitations_per_day: {
    source: "deployment",
    deployment_default: 30,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "个 / 滚动 24 小时",
    effect: "下一次创建邀请时读取，缓存传播最多约 30 秒。",
    min_value: 1,
    max_value: 1000,
    in_range: true,
  },
  offline_threshold_minutes: {
    source: "deployment",
    deployment_default: 5,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "分钟",
    effect: "下一轮在线状态扫描采用新值，扫描调度仍保持原周期。",
    min_value: 2,
    max_value: 60,
    in_range: true,
  },
  frontend_base_url: {
    source: "deployment",
    deployment_default: "http://localhost:5173",
    updated_at: null,
    updated_by: null,
    value_type: "str",
    unit: null,
    effect: "只影响之后生成的链接。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
  dataset_import_max_files: {
    source: "deployment",
    deployment_default: 50000,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "个文件",
    effect: "后续新导入在 API 受理时读取；排队任务保留自己的快照。",
    min_value: 1,
    max_value: 50000,
    in_range: true,
  },
  dataset_import_max_total_bytes: {
    source: "deployment",
    deployment_default: 214748364800,
    updated_at: null,
    updated_by: null,
    value_type: "bytes",
    unit: "bytes",
    effect: "与文件数一起在受理时读取并写入导入任务快照。",
    min_value: 1,
    max_value: 214748364800,
    in_range: true,
  },
  task_create_sync_threshold: {
    source: "deployment",
    deployment_default: 2000,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "条数据",
    effect: "下一次关联请求决定分支，不改变已经排队的任务。",
    min_value: 0,
    max_value: 2000,
    in_range: true,
  },
  video_chunk_warmup_lookahead: {
    source: "deployment",
    deployment_default: 1,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "个块",
    effect: "只影响新的视频请求，不取消已排队的预热。",
    min_value: 0,
    max_value: 1,
    in_range: true,
  },
  smtp_host: {
    source: "deployment",
    deployment_default: null,
    updated_at: null,
    updated_by: null,
    value_type: "str",
    unit: null,
    effect: "SMTP 服务器主机名。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
  smtp_port: {
    source: "deployment",
    deployment_default: null,
    updated_at: null,
    updated_by: null,
    value_type: "int",
    unit: "端口",
    effect: "SMTP 服务器端口。",
    min_value: 1,
    max_value: 65535,
    in_range: true,
  },
  smtp_user: {
    source: "deployment",
    deployment_default: null,
    updated_at: null,
    updated_by: null,
    value_type: "str",
    unit: null,
    effect: "SMTP 登录用户名。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
  smtp_password: {
    source: "deployment",
    deployment_default: false,
    updated_at: null,
    updated_by: null,
    value_type: "str",
    unit: null,
    effect: "SMTP 登录密码；只返回是否已设置。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
  smtp_from: {
    source: "deployment",
    deployment_default: null,
    updated_at: null,
    updated_by: null,
    value_type: "str",
    unit: null,
    effect: "SMTP 发件人地址。",
    min_value: null,
    max_value: null,
    in_range: true,
  },
};

function asRecord(data: SystemSettingsResponse): Record<string, unknown> {
  return data as unknown as Record<string, unknown>;
}

function responseValue(data: SystemSettingsResponse, key: SystemSettingKey): unknown {
  return asRecord(data)[key];
}

function settingMetadataForKey(
  data: SystemSettingsResponse,
  key: SystemSettingKey,
): SystemSettingMetadata {
  const fallback = DEFAULT_METADATA[key];
  const value = data.metadata?.[key];
  return {
    source: value?.source ?? fallback.source ?? "deployment",
    deployment_default: value?.deployment_default ?? fallback.deployment_default,
    updated_at: value?.updated_at ?? fallback.updated_at ?? null,
    updated_by: value?.updated_by ?? fallback.updated_by ?? null,
    value_type: value?.value_type ?? fallback.value_type,
    unit: value?.unit ?? fallback.unit,
    effect: value?.effect ?? fallback.effect,
    min_value: value?.min_value ?? fallback.min_value,
    max_value: value?.max_value ?? fallback.max_value,
    in_range: value?.in_range ?? fallback.in_range ?? true,
  };
}

function settingMetadata(
  data: SystemSettingsResponse,
  definition: SettingDefinition,
): SystemSettingMetadata {
  return settingMetadataForKey(data, definition.key);
}

function integerString(value: unknown): string | null {
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "number" && Number.isInteger(value) && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "string" && /^\d+$/.test(value.trim())) return value.trim();
  return null;
}

/** Convert a byte count to a terminating, exact base-10 GiB string. */
export function bytesToGib(value: number | string | bigint): string {
  const bytes = integerString(value);
  if (bytes == null) throw new Error("字节数必须是非负整数");
  const integer = BigInt(bytes);
  const whole = integer / BYTES_PER_GIB;
  let remainder = integer % BYTES_PER_GIB;
  if (remainder === 0n) return whole.toString();

  // 2^30 has a finite decimal expansion with at most 30 fractional digits.
  let fraction = "";
  for (let i = 0; i < 30 && remainder !== 0n; i += 1) {
    remainder *= 10n;
    fraction += (remainder / BYTES_PER_GIB).toString();
    remainder %= BYTES_PER_GIB;
  }
  return `${whole.toString()}.${fraction.replace(/0+$/, "")}`;
}

/** Convert a decimal GiB input into an integer byte count without Number rounding. */
export function gibToBytes(value: string): number {
  const normalized = value.trim();
  const match = /^(\d+)(?:\.(\d+))?$/.exec(normalized);
  if (!match) throw new Error("请输入非负数字 GiB");
  const whole = BigInt(match[1]);
  const fraction = match[2] ?? "";
  const scale = 10n ** BigInt(fraction.length);
  const numerator = (whole * scale + BigInt(fraction || "0")) * BYTES_PER_GIB;
  if (numerator % scale !== 0n) {
    throw new Error("GiB 小数位无法精确换算为整数 bytes");
  }
  const bytes = numerator / scale;
  if (bytes > MAX_SAFE_INTEGER_BIGINT) {
    throw new Error("数值超过浏览器可精确提交范围");
  }
  return Number(bytes);
}

function draftValue(data: SystemSettingsResponse, definition: SettingDefinition): DraftValue {
  const metadata = settingMetadata(data, definition);
  const raw = responseValue(data, definition.key);
  const value = raw ?? metadata.deployment_default ?? definition.fallbackDefault;
  if (definition.input === "boolean") return value === true;
  if (definition.input === "bytes") {
    const bytes = integerString(value);
    return bytes == null ? "" : bytesToGib(bytes);
  }
  return value == null ? "" : String(value);
}

function initialDrafts(data: SystemSettingsResponse): Drafts {
  return Object.fromEntries(
    SETTING_DEFINITIONS.map((definition) => [definition.key, draftValue(data, definition)]),
  ) as Drafts;
}

function initialSmtpDraft(data: SystemSettingsResponse): SmtpDraft {
  return {
    host: data.smtp.host ?? "",
    port: data.smtp.port == null ? "" : String(data.smtp.port),
    user: data.smtp.user ?? "",
    from: data.smtp.from_address ?? "",
    passwordMode: "preserve",
    password: "",
  };
}

function sameDraft(a: DraftValue | undefined, b: DraftValue | undefined): boolean {
  return a === b;
}

function sameSmtpDraft(a: SmtpDraft, b: SmtpDraft): boolean {
  return (
    a.host === b.host &&
    a.port === b.port &&
    a.user === b.user &&
    a.from === b.from &&
    a.passwordMode === b.passwordMode &&
    a.password === b.password
  );
}

function apiConflict(error: unknown): boolean {
  return error instanceof ApiError
    ? error.status === 409
    : Boolean(error && typeof error === "object" && (error as { status?: unknown }).status === 409);
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function formatBytes(value: unknown): string {
  const bytes = integerString(value);
  if (bytes == null) return "—";
  try {
    return `${bytesToGib(bytes)} GiB（${bytes} bytes）`;
  } catch {
    return `${bytes} bytes`;
  }
}

function formatEffectiveValue(
  definition: SettingDefinition,
  value: unknown,
  metadata: SystemSettingMetadata,
): string {
  const actual = value ?? metadata.deployment_default;
  if (definition.input === "boolean") return actual === true ? "已启用" : "已关闭";
  if (definition.input === "bytes") return formatBytes(actual);
  if (actual == null || actual === "") return "—";
  const unit = metadata.unit ?? definition.fallbackUnit;
  return unit ? `${String(actual)} ${unit}` : String(actual);
}

function formatDraftValue(definition: SettingDefinition, value: DraftValue | undefined): string {
  if (definition.input === "boolean") return value === true ? "已启用" : "已关闭";
  if (value == null || value === "") return "—";
  if (definition.input === "bytes") return `${String(value)} GiB`;
  return String(value);
}

function rangeLabel(metadata: SystemSettingMetadata): string {
  if (metadata.min_value == null && metadata.max_value == null) return "不设数值范围";
  if (metadata.min_value == null) return `不超过 ${metadata.max_value}`;
  if (metadata.max_value == null) return `不少于 ${metadata.min_value}`;
  return `${metadata.min_value}–${metadata.max_value}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请稍后重试";
}

function cloneSmtp(value: SmtpDraft): SmtpDraft {
  return { ...value };
}

function matchesSmtpSearch(search: string): boolean {
  const needle = search.trim().toLocaleLowerCase();
  return (
    !needle ||
    [
      "smtp",
      "邮件",
      "访问地址",
      "主机",
      "端口",
      "账号",
      "用户",
      "发件人",
      "密码",
      "smtp_host",
      "smtp_port",
      "smtp_user",
      "smtp_password",
      "smtp_from",
    ].some((term) => term.includes(needle))
  );
}

function ErrorBanner({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-status-danger/40 bg-status-danger-soft px-3 py-2 text-sm text-foreground"
    >
      {typeof children === "string" ? <CopyableError message={children} /> : children}
    </div>
  );
}

export function SystemSettingsSection({ onDirtyChange }: SystemSettingsSectionProps) {
  const query = useSystemSettings();
  const { data, isLoading, error, refetch } = query;
  const updateMut = useUpdateSystemSettings();
  const resetMut = useResetSystemSettings();
  const testSmtpMut = useTestSmtp();
  const pushToast = useToastStore((state) => state.push);
  const [search, setSearch] = useState("");
  const [drafts, setDraftsState] = useState<Drafts>({});
  const [baseline, setBaseline] = useState<Drafts>({});
  const [smtpDraft, setSmtpDraftState] = useState<SmtpDraft>({
    host: "",
    port: "",
    user: "",
    from: "",
    passwordMode: "preserve",
    password: "",
  });
  const [smtpBaseline, setSmtpBaseline] = useState<SmtpDraft>(smtpDraft);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [resettingKey, setResettingKey] = useState<SystemSettingKey | null>(null);
  const initializedRef = useRef(false);
  const baselineVersionRef = useRef<string | undefined>(undefined);
  const draftsRef = useRef<Drafts>({});
  const baselineRef = useRef<Drafts>({});
  const smtpDraftRef = useRef(smtpDraft);
  const smtpBaselineRef = useRef(smtpBaseline);

  const setDrafts = useCallback((next: Drafts) => {
    draftsRef.current = next;
    setDraftsState(next);
  }, []);
  const setSmtpDraft = useCallback((next: SmtpDraft) => {
    smtpDraftRef.current = next;
    setSmtpDraftState(next);
  }, []);

  const applyServerData = useCallback(
    (
      nextData: SystemSettingsResponse,
      preserveDirty = false,
      preserveKey?: SystemSettingKey,
      savedGroup?: SettingGroup,
    ) => {
      const nextBaseline = initialDrafts(nextData);
      const nextSmtpBaseline = initialSmtpDraft(nextData);
      const preservedDrafts: Drafts = {};
      if (preserveDirty) {
        for (const definition of SETTING_DEFINITIONS) {
          if (
            definition.key !== preserveKey &&
            definition.group !== savedGroup &&
            !sameDraft(draftsRef.current[definition.key], baselineRef.current[definition.key])
          ) {
            preservedDrafts[definition.key] = draftsRef.current[definition.key];
          }
        }
      }
      let preservedSmtp = nextSmtpBaseline;
      if (
        preserveDirty &&
        savedGroup !== "mail" &&
        !sameSmtpDraft(smtpDraftRef.current, smtpBaselineRef.current)
      ) {
        preservedSmtp = cloneSmtp(smtpDraftRef.current);
        if (preserveKey === "smtp_host") preservedSmtp.host = nextSmtpBaseline.host;
        if (preserveKey === "smtp_port") preservedSmtp.port = nextSmtpBaseline.port;
        if (preserveKey === "smtp_user") preservedSmtp.user = nextSmtpBaseline.user;
        if (preserveKey === "smtp_from") preservedSmtp.from = nextSmtpBaseline.from;
        if (preserveKey === "smtp_password") {
          preservedSmtp.passwordMode = "preserve";
          preservedSmtp.password = "";
        }
      }
      const nextDrafts = preserveDirty ? { ...nextBaseline, ...preservedDrafts } : nextBaseline;
      baselineRef.current = nextBaseline;
      baselineVersionRef.current = nextData.version;
      smtpBaselineRef.current = nextSmtpBaseline;
      setBaseline(nextBaseline);
      setSmtpBaseline(nextSmtpBaseline);
      setDrafts(nextDrafts);
      setSmtpDraft(preservedSmtp);
      initializedRef.current = true;
      setConflict(false);
      setSaveError(null);
    },
    [setDrafts, setSmtpDraft],
  );

  useEffect(() => {
    if (!data) return;
    const nextBaseline = initialDrafts(data);
    const nextSmtpBaseline = initialSmtpDraft(data);
    const isDirty =
      initializedRef.current &&
      (SETTING_DEFINITIONS.some(
        (definition) =>
          !sameDraft(draftsRef.current[definition.key], baselineRef.current[definition.key]),
      ) ||
        !sameSmtpDraft(smtpDraftRef.current, smtpBaselineRef.current));

    // Query refreshes update the readback/provenance surface, but never replace a
    // draft the user is actively editing. A clean form follows the latest server data.
    if (isDirty) {
      if (data.version !== baselineVersionRef.current) setConflict(true);
      return;
    }
    baselineRef.current = nextBaseline;
    baselineVersionRef.current = data.version;
    smtpBaselineRef.current = nextSmtpBaseline;
    setBaseline(nextBaseline);
    setSmtpBaseline(nextSmtpBaseline);
    if (!isDirty || !initializedRef.current) {
      setDrafts(nextBaseline);
      setSmtpDraft(nextSmtpBaseline);
    }
    initializedRef.current = true;
  }, [data, setDrafts, setSmtpDraft]);

  const dirtyKeys = useMemo(
    () =>
      SETTING_DEFINITIONS.filter(
        (definition) => !sameDraft(drafts[definition.key], baseline[definition.key]),
      ).map((definition) => definition.key),
    [baseline, drafts],
  );
  const smtpDirty = useMemo(
    () => !sameSmtpDraft(smtpDraft, smtpBaseline),
    [smtpDraft, smtpBaseline],
  );
  const allDirty = dirtyKeys.length > 0 || smtpDirty;

  useEffect(() => {
    onDirtyChange?.(allDirty);
    return () => onDirtyChange?.(false);
  }, [allDirty, onDirtyChange]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!allDirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [allDirty]);

  const visibleDefinitions = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    if (!needle) return SETTING_DEFINITIONS;
    return SETTING_DEFINITIONS.filter((definition) => {
      const metadata = data ? settingMetadata(data, definition) : null;
      return [
        definition.label,
        definition.key,
        GROUPS.find((group) => group.key === definition.group)?.label,
        definition.description,
        metadata?.effect,
        metadata?.unit,
      ]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle);
    });
  }, [data, search]);

  const resetKey = useCallback(
    (key: SystemSettingKey) => {
      if (!data || resettingKey) return;
      setResettingKey(key);
      resetMut.mutate(
        {
          keys: [key],
          ...(baselineVersionRef.current ? { expected_version: baselineVersionRef.current } : {}),
        },
        {
          onSuccess: (saved) => {
            applyServerData(saved, true, key);
            pushToast({ msg: "已恢复部署默认", kind: "success" });
            setResettingKey(null);
          },
          onError: async (resetError) => {
            if (apiConflict(resetError)) {
              setConflict(true);
              await refetch?.();
            }
            pushToast({ msg: "恢复默认失败", sub: errorMessage(resetError), kind: "warning" });
            setResettingKey(null);
          },
        },
      );
    },
    [applyServerData, data, pushToast, refetch, resetMut, resettingKey],
  );

  const validateAndPatchValue = useCallback(
    (definition: SettingDefinition, value: DraftValue): SystemSettingsPatch | string => {
      if (definition.input === "boolean") return { [definition.key]: value === true };
      if (typeof value !== "string" || value.trim() === "") return `${definition.label}不能为空`;
      if (definition.input === "string") return { [definition.key]: value.trim() };
      const metadata = data ? settingMetadata(data, definition) : DEFAULT_METADATA[definition.key];
      if (definition.input === "bytes") {
        try {
          const bytes = gibToBytes(value);
          if (
            (metadata.min_value != null && bytes < metadata.min_value) ||
            (metadata.max_value != null && bytes > metadata.max_value)
          ) {
            return `${definition.label}需在 ${rangeLabel(metadata)} bytes 范围内`;
          }
          return { [definition.key]: bytes };
        } catch (validationError) {
          return `${definition.label}：${errorMessage(validationError)}`;
        }
      }
      if (definition.input === "integer" && !/^\d+$/.test(value.trim())) {
        return `${definition.label}必须是整数`;
      }
      const numeric = Number(value);
      if (!Number.isSafeInteger(numeric)) return `${definition.label}超过可精确提交范围`;
      if (
        (metadata.min_value != null && numeric < metadata.min_value) ||
        (metadata.max_value != null && numeric > metadata.max_value)
      ) {
        return `${definition.label}需在 ${rangeLabel(metadata)} 范围内`;
      }
      return { [definition.key]: numeric };
    },
    [data],
  );

  const saveGroup = useCallback(
    (group: SettingGroup, event?: FormEvent) => {
      event?.preventDefault();
      if (!data || updateMut.isPending) return;
      const patch: SystemSettingsPatch = {
        ...(baselineVersionRef.current ? { expected_version: baselineVersionRef.current } : {}),
      };
      const changedDefinitions = SETTING_DEFINITIONS.filter(
        (definition) =>
          definition.group === group &&
          !sameDraft(drafts[definition.key], baseline[definition.key]),
      );
      for (const definition of changedDefinitions) {
        const result = validateAndPatchValue(definition, drafts[definition.key] as DraftValue);
        if (typeof result === "string") {
          pushToast({ msg: "无法保存设置", sub: result, kind: "warning" });
          return;
        }
        Object.assign(patch, result);
      }
      if (group === "mail") {
        const original = smtpBaselineRef.current;
        if (smtpDraft.host !== original.host) patch.smtp_host = smtpDraft.host.trim();
        if (smtpDraft.port !== original.port) {
          if (!/^\d+$/.test(smtpDraft.port.trim())) {
            pushToast({ msg: "无法保存设置", sub: "SMTP 端口必须是整数", kind: "warning" });
            return;
          } else {
            const port = Number(smtpDraft.port);
            if (!Number.isInteger(port) || port < 1 || port > 65535) {
              pushToast({
                msg: "无法保存设置",
                sub: "SMTP 端口需在 1–65535 之间",
                kind: "warning",
              });
              return;
            }
            patch.smtp_port = port;
          }
        }
        if (smtpDraft.user !== original.user) patch.smtp_user = smtpDraft.user.trim();
        if (smtpDraft.from !== original.from) patch.smtp_from = smtpDraft.from.trim();
        if (smtpDraft.passwordMode === "change") {
          if (!smtpDraft.password) {
            pushToast({ msg: "无法保存设置", sub: "请输入 SMTP 新密码", kind: "warning" });
            return;
          }
          patch.smtp_password = smtpDraft.password;
        } else if (smtpDraft.passwordMode === "clear") {
          patch.smtp_password = "";
        }
      }
      const { expected_version: _expectedVersion, ...changes } = patch;
      if (Object.keys(changes).length === 0) return;

      setSaveError(null);
      updateMut.mutate(patch, {
        onSuccess: (saved) => {
          // The server response is the source of truth: in particular it only
          // exposes password_set, never the password that was submitted.
          applyServerData(saved, true, undefined, group);
          pushToast({
            msg: `${GROUPS.find((item) => item.key === group)?.label ?? "设置"}已保存`,
            kind: "success",
          });
        },
        onError: async (saveErrorValue) => {
          if (apiConflict(saveErrorValue)) {
            setConflict(true);
            setSaveError(
              "设置已在其他窗口更新；已保留当前草稿，下面的当前值会在刷新后显示最新服务器值。",
            );
            await refetch?.();
          } else {
            setSaveError(errorMessage(saveErrorValue));
          }
        },
      });
    },
    [
      applyServerData,
      baseline,
      data,
      drafts,
      pushToast,
      refetch,
      smtpDraft,
      updateMut,
      validateAndPatchValue,
    ],
  );

  const cancelGroup = useCallback(
    (group: SettingGroup) => {
      const groupKeys = SETTING_DEFINITIONS.filter((definition) => definition.group === group).map(
        (definition) => definition.key,
      );
      const nextDrafts = { ...draftsRef.current };
      for (const key of groupKeys) nextDrafts[key] = baselineRef.current[key];
      setDrafts(nextDrafts);
      if (group === "mail") setSmtpDraft(cloneSmtp(smtpBaselineRef.current));
      setSaveError(null);
    },
    [setDrafts, setSmtpDraft],
  );

  const handleSmtpTest = useCallback(() => {
    if (smtpDirty || dirtyKeys.includes("frontend_base_url") || !data?.smtp.configured) return;
    testSmtpMut.mutate(undefined, {
      onSuccess: (result) =>
        pushToast({
          msg: "测试邮件已发送",
          sub: result.to ? `→ ${result.to}` : undefined,
          kind: "success",
        }),
      onError: (testError) =>
        pushToast({ msg: "SMTP 测试失败", sub: errorMessage(testError), kind: "warning" }),
    });
  }, [data?.smtp.configured, dirtyKeys, pushToast, smtpDirty, testSmtpMut]);

  if (isLoading || !data) {
    return (
      <Card>
        <div className="border-b border-border px-4 py-3 text-sm font-semibold">系统设置</div>
        <div className="p-4 text-sm text-muted-foreground">
          {isLoading ? "加载中..." : null}
          {error && <ErrorBanner>{errorMessage(error)}</ErrorBanner>}
        </div>
      </Card>
    );
  }

  const renderGroup = (group: (typeof GROUPS)[number]) => {
    const definitions = visibleDefinitions.filter((definition) => definition.group === group.key);
    const mailSearchMatch = matchesSmtpSearch(search);
    const groupKeys = SETTING_DEFINITIONS.filter(
      (definition) => definition.group === group.key,
    ).map((definition) => definition.key);
    const groupDirty =
      groupKeys.some((key) => dirtyKeys.includes(key)) || (group.key === "mail" && smtpDirty);
    if (search.trim() && definitions.length === 0 && (group.key !== "mail" || !mailSearchMatch)) {
      return null;
    }

    return (
      <Card key={group.key}>
        <div className={GROUP_HEADER_CLASS}>
          <div>
            <h3 className="m-0 text-sm font-semibold text-foreground">{group.label}</h3>
            <p className="m-0 mt-1 text-xs text-muted-foreground">{group.description}</p>
          </div>
          <Badge variant={groupDirty ? "warning" : "outline"}>
            {groupDirty ? "有未保存修改" : "已同步"}
          </Badge>
        </div>
        <form className={FORM_CLASS} onSubmit={(event) => saveGroup(group.key, event)}>
          <fieldset disabled={updateMut.isPending || resetMut.isPending} className="contents">
            {definitions.map((definition) => (
              <SettingRow
                key={definition.key}
                data={data}
                definition={definition}
                metadata={settingMetadata(data, definition)}
                draft={drafts[definition.key]}
                baseline={baseline[definition.key]}
                onChange={(value) => setDrafts({ ...draftsRef.current, [definition.key]: value })}
                onReset={() => resetKey(definition.key)}
                resetting={resettingKey === definition.key}
              />
            ))}
            {group.key === "mail" && mailSearchMatch && (
              <SmtpFields
                data={data}
                draft={smtpDraft}
                dirty={groupDirty}
                onChange={setSmtpDraft}
                onTest={handleSmtpTest}
                testPending={testSmtpMut.isPending}
                onReset={resetKey}
                resettingKey={resettingKey}
              />
            )}
            {groupDirty && (
              <ChangeSummary
                data={data}
                definitions={SETTING_DEFINITIONS.filter(
                  (definition) => definition.group === group.key,
                )}
                drafts={drafts}
                baseline={baseline}
                smtpDraft={smtpDraft}
                smtpBaseline={smtpBaseline}
              />
            )}
            <div className="flex flex-wrap items-center justify-end gap-2">
              <button
                type="button"
                className={BUTTON_CLASS}
                disabled={!groupDirty || updateMut.isPending}
                onClick={() => cancelGroup(group.key)}
              >
                取消修改
              </button>
              <button
                type="submit"
                className={PRIMARY_BUTTON_CLASS}
                disabled={!groupDirty || updateMut.isPending}
              >
                {updateMut.isPending ? "保存中..." : `保存${group.label}`}
              </button>
            </div>
          </fieldset>
        </form>
      </Card>
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <h2 className="m-0 text-base font-semibold">系统设置</h2>
            <p className="m-0 mt-1 text-xs text-muted-foreground">
              仅 super_admin 可修改；保存后由服务器读回实际生效值。部署默认来自当前环境，不会写回
              .env。
            </p>
          </div>
          <Badge variant="outline">环境：{data.environment}</Badge>
        </div>
        <div className="border-t border-border px-4 py-3">
          <label className="relative block" htmlFor="system-settings-search">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <input
              id="system-settings-search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索设置名称、变量名或作用"
              className={clsx(INPUT_CLASS, "pl-9")}
            />
          </label>
          <div className="mt-2 text-xs text-muted-foreground">
            {data.version
              ? `配置版本 ${data.version}`
              : "旧版本 API 未提供配置版本，保存时不启用并发校验"}
          </div>
        </div>
      </Card>

      {(saveError || conflict) && (
        <div className="flex flex-col gap-2">
          {saveError && <ErrorBanner>{saveError}</ErrorBanner>}
          {conflict && (
            <div className="rounded-md border border-status-warning/40 bg-status-warning-soft px-3 py-2 text-sm text-foreground">
              最新服务器值已显示在每一项的“当前有效值”中；你的草稿仍保留，请核对差异。
              <button
                type="button"
                className={`${BUTTON_CLASS} mt-2`}
                onClick={() => applyServerData(data, true)}
              >
                已核对最新值，保留草稿继续编辑
              </button>
            </div>
          )}
        </div>
      )}

      {GROUPS.map(renderGroup)}
      {search.trim() && visibleDefinitions.length === 0 && !matchesSmtpSearch(search) && (
        <Card>
          <div className="p-6 text-center text-sm text-muted-foreground">没有匹配的系统设置</div>
        </Card>
      )}
    </div>
  );
}

function SettingRow({
  data,
  definition,
  metadata,
  draft,
  baseline,
  onChange,
  onReset,
  resetting,
}: {
  data: SystemSettingsResponse;
  definition: SettingDefinition;
  metadata: SystemSettingMetadata;
  draft: DraftValue | undefined;
  baseline: DraftValue | undefined;
  onChange: (value: DraftValue) => void;
  onReset: () => void;
  resetting: boolean;
}) {
  const id = `system-setting-${definition.key}`;
  const changed = !sameDraft(draft, baseline);
  const effective = formatEffectiveValue(definition, responseValue(data, definition.key), metadata);
  const sourceLabel = metadata.source === "override" ? "后台覆盖" : "部署默认";
  const range = rangeLabel(metadata);
  return (
    <div
      className={clsx("rounded-md border border-border p-3", changed && "border-status-warning/60")}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={id} className="text-sm font-medium text-foreground">
              {definition.label}
            </label>
            <Badge variant={metadata.source === "override" ? "accent" : "outline"}>
              {sourceLabel}
            </Badge>
            {!metadata.in_range && <Badge variant="warning">当前值超出编辑范围</Badge>}
          </div>
          <p className="m-0 mt-1 text-xs leading-5 text-muted-foreground">
            {definition.description}
          </p>
        </div>
        <button type="button" className={BUTTON_CLASS} onClick={onReset} disabled={resetting}>
          <Icon name="rotate-ccw" size={13} />
          {resetting ? "恢复中..." : "恢复部署默认"}
        </button>
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(230px,1fr)]">
        <div>
          <div className={FIELD_LABEL_CLASS}>
            编辑值
            {definition.input === "bytes" ? "（GiB）" : metadata.unit ? `（${metadata.unit}）` : ""}
          </div>
          {definition.input === "boolean" ? (
            <label className="inline-flex min-h-10 cursor-pointer items-center gap-2 text-sm text-foreground">
              <input
                id={id}
                type="checkbox"
                checked={draft === true}
                onChange={(event) => onChange(event.target.checked)}
              />
              <span>
                {definition.key === "allow_open_registration"
                  ? draft === true
                    ? "已启用 — 新用户自助注册为 Viewer"
                    : "已关闭 — 仅邀请注册"
                  : draft === true
                    ? "已启用"
                    : "已关闭"}
              </span>
            </label>
          ) : (
            <input
              id={id}
              type={definition.input === "integer" ? "number" : "text"}
              inputMode={
                definition.input === "bytes"
                  ? "decimal"
                  : definition.input === "string"
                    ? "url"
                    : "numeric"
              }
              value={typeof draft === "boolean" || draft == null ? "" : draft}
              onChange={(event) => onChange(event.target.value)}
              className={INPUT_CLASS}
              aria-describedby={`${id}-hint`}
            />
          )}
          <div id={`${id}-hint`} className="mt-1 text-xs text-muted-foreground">
            范围：{range}
            {definition.input === "bytes" && " bytes（编辑时使用 GiB，保存精确换算）"}
          </div>
        </div>
        <div className="rounded-md bg-muted/50 px-3 py-2 text-xs leading-5 text-muted-foreground">
          <div>
            <span className="font-medium text-foreground">当前有效值：</span>
            {effective}
          </div>
          <div>
            来源：{sourceLabel} · 修改人：{metadata.updated_by ?? "—"} · 时间：
            {formatDate(metadata.updated_at)}
          </div>
          <div>生效说明：{metadata.effect || definition.fallbackEffect}</div>
          <div className="font-mono text-[0.7rem]">{definition.key}</div>
        </div>
      </div>
    </div>
  );
}

function SmtpFields({
  data,
  draft,
  dirty,
  onChange,
  onTest,
  testPending,
  onReset,
  resettingKey,
}: {
  data: SystemSettingsResponse;
  draft: SmtpDraft;
  dirty: boolean;
  onChange: (value: SmtpDraft) => void;
  onTest: () => void;
  testPending: boolean;
  onReset: (key: SystemSettingKey) => void;
  resettingKey: SystemSettingKey | null;
}) {
  const update = <K extends keyof SmtpDraft>(key: K, value: SmtpDraft[K]) =>
    onChange({ ...draft, [key]: value });
  return (
    <div className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 text-sm font-medium">SMTP 邮件</h3>
        <Badge variant={data.smtp.configured ? "success" : "outline"} dot>
          {data.smtp.configured ? "已配置" : "未配置"}
        </Badge>
        <span className="text-xs text-muted-foreground">密码只返回是否设置，不会回显。</span>
      </div>
      <div className="mt-3 grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <div className="text-sm text-foreground">
          <label className={FIELD_LABEL_CLASS} htmlFor="smtp-host">
            主机
          </label>
          <input
            id="smtp-host"
            value={draft.host}
            onChange={(event) => update("host", event.target.value)}
            className={INPUT_CLASS}
            placeholder="smtp.example.com"
          />
          <SmtpMeta
            data={data}
            settingKey="smtp_host"
            onReset={onReset}
            resetting={resettingKey === "smtp_host"}
          />
        </div>
        <div className="text-sm text-foreground">
          <label className={FIELD_LABEL_CLASS} htmlFor="smtp-port">
            端口
          </label>
          <input
            id="smtp-port"
            value={draft.port}
            onChange={(event) => update("port", event.target.value)}
            className={INPUT_CLASS}
            inputMode="numeric"
            placeholder="587 / 465"
          />
          <SmtpMeta
            data={data}
            settingKey="smtp_port"
            onReset={onReset}
            resetting={resettingKey === "smtp_port"}
          />
        </div>
        <div className="text-sm text-foreground">
          <label className={FIELD_LABEL_CLASS} htmlFor="smtp-user">
            账号
          </label>
          <input
            id="smtp-user"
            value={draft.user}
            onChange={(event) => update("user", event.target.value)}
            className={INPUT_CLASS}
          />
          <SmtpMeta
            data={data}
            settingKey="smtp_user"
            onReset={onReset}
            resetting={resettingKey === "smtp_user"}
          />
        </div>
        <div className="text-sm text-foreground">
          <label className={FIELD_LABEL_CLASS} htmlFor="smtp-from">
            发件人
          </label>
          <input
            id="smtp-from"
            value={draft.from}
            onChange={(event) => update("from", event.target.value)}
            className={INPUT_CLASS}
            placeholder="noreply@example.com"
          />
          <SmtpMeta
            data={data}
            settingKey="smtp_from"
            onReset={onReset}
            resetting={resettingKey === "smtp_from"}
          />
        </div>
      </div>
      <div className="mt-3">
        <div className={FIELD_LABEL_CLASS}>SMTP 密码</div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={clsx(
              BUTTON_CLASS,
              draft.passwordMode === "preserve" && "border-primary text-primary",
            )}
            onClick={() => update("passwordMode", "preserve")}
          >
            保留已保存密码
          </button>
          <button
            type="button"
            className={clsx(
              BUTTON_CLASS,
              draft.passwordMode === "change" && "border-primary text-primary",
            )}
            onClick={() => update("passwordMode", "change")}
          >
            更换密码
          </button>
          <button
            type="button"
            className={clsx(
              BUTTON_CLASS,
              draft.passwordMode === "clear" && "border-status-danger text-status-danger",
            )}
            onClick={() => update("passwordMode", "clear")}
          >
            清除密码
          </button>
        </div>
        {draft.passwordMode === "change" && (
          <input
            type="password"
            value={draft.password}
            onChange={(event) => update("password", event.target.value)}
            className={clsx(INPUT_CLASS, "mt-2")}
            placeholder="输入新密码；不会显示旧密码"
            autoComplete="new-password"
          />
        )}
        <p className="m-0 mt-1 text-xs text-muted-foreground">
          当前状态：{data.smtp.password_set ? "已设置" : "未设置"}
          。保留不会发送密码字段，清除会显式删除已保存密码。
        </p>
        <SmtpMeta
          data={data}
          settingKey="smtp_password"
          effective={data.smtp.password_set ? "已设置" : "未设置"}
          onReset={onReset}
          resetting={resettingKey === "smtp_password"}
        />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onTest}
          disabled={testPending || !data.smtp.configured || dirty}
          className={BUTTON_CLASS}
          title={dirty ? "请先保存邮件与访问地址设置" : undefined}
        >
          {testPending ? "发送中..." : dirty ? "先保存后测试邮件" : "发送测试邮件到我"}
        </button>
        <span className="text-xs text-muted-foreground">
          {data.smtp.configured ? "收件人：当前账号邮箱，使用已保存配置" : "请先保存完整 SMTP 配置"}
        </span>
      </div>
    </div>
  );
}

function SmtpMeta({
  data,
  settingKey,
  effective,
  onReset,
  resetting,
}: {
  data: SystemSettingsResponse;
  settingKey: Extract<SystemSettingKey, `smtp_${string}`>;
  effective?: string;
  onReset: (key: SystemSettingKey) => void;
  resetting: boolean;
}) {
  const metadata = settingMetadataForKey(data, settingKey);
  const sourceLabel = metadata.source === "override" ? "后台覆盖" : "部署默认";
  const current =
    effective ??
    (settingKey === "smtp_host"
      ? (data.smtp.host ?? "—")
      : settingKey === "smtp_port"
        ? data.smtp.port == null
          ? "—"
          : String(data.smtp.port)
        : settingKey === "smtp_user"
          ? (data.smtp.user ?? "—")
          : (data.smtp.from_address ?? "—"));
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.7rem] leading-4 text-muted-foreground">
      <Badge variant={metadata.source === "override" ? "accent" : "outline"}>{sourceLabel}</Badge>
      <span>当前：{current}</span>
      {metadata.unit && <span>单位：{metadata.unit}</span>}
      {metadata.min_value != null || metadata.max_value != null ? (
        <span>范围：{rangeLabel(metadata)}</span>
      ) : null}
      <span>
        修改人：{metadata.updated_by ?? "—"} · {formatDate(metadata.updated_at)}
      </span>
      <span>作用：{metadata.effect}</span>
      <button
        type="button"
        className="cursor-pointer border-0 bg-transparent p-0 text-[0.7rem] text-primary underline-offset-2 hover:underline disabled:cursor-not-allowed disabled:opacity-60"
        onClick={() => onReset(settingKey)}
        disabled={resetting}
      >
        {resetting ? "恢复中..." : "恢复部署默认"}
      </button>
    </div>
  );
}

function ChangeSummary({
  data,
  definitions,
  drafts,
  baseline,
  smtpDraft,
  smtpBaseline,
}: {
  data: SystemSettingsResponse;
  definitions: SettingDefinition[];
  drafts: Drafts;
  baseline: Drafts;
  smtpDraft: SmtpDraft;
  smtpBaseline: SmtpDraft;
}) {
  const changes = definitions
    .filter((definition) => !sameDraft(drafts[definition.key], baseline[definition.key]))
    .map((definition) => (
      <li key={definition.key}>
        {definition.label}：{formatDraftValue(definition, baseline[definition.key])} →{" "}
        {formatDraftValue(definition, drafts[definition.key])}
      </li>
    ));
  if (smtpDraft.host !== smtpBaseline.host) changes.push(<li key="smtp-host">SMTP 主机已修改</li>);
  if (smtpDraft.port !== smtpBaseline.port) changes.push(<li key="smtp-port">SMTP 端口已修改</li>);
  if (smtpDraft.user !== smtpBaseline.user) changes.push(<li key="smtp-user">SMTP 账号已修改</li>);
  if (smtpDraft.from !== smtpBaseline.from)
    changes.push(<li key="smtp-from">SMTP 发件人已修改</li>);
  if (
    smtpDraft.passwordMode !== smtpBaseline.passwordMode ||
    smtpDraft.password !== smtpBaseline.password
  ) {
    changes.push(
      <li key="smtp-password">
        SMTP 密码：
        {smtpDraft.passwordMode === "clear"
          ? "清除"
          : smtpDraft.passwordMode === "change"
            ? "更换（秘密不会显示）"
            : "保留"}
      </li>,
    );
  }
  return (
    <div className="rounded-md border border-status-info/40 bg-status-info-soft px-3 py-2 text-sm text-foreground">
      <div className="font-medium">本组待保存变更</div>
      <ul className="m-0 mt-1 list-disc pl-5 text-xs leading-5">{changes}</ul>
      {data.version && (
        <div className="mt-1 text-xs text-muted-foreground">保存会校验版本 {data.version}。</div>
      )}
    </div>
  );
}
