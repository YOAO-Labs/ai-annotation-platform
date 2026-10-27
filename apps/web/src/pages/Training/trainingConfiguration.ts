import { isAlias, isMap, isNode, isScalar, parseAllDocuments, stringify, visit } from "yaml";
import type { TrainingParameterField, TrainingParameterSchema } from "@/api/generated/types.gen";
import type { TrainingInitialization } from "@/api/trainingRuns";
import { validInitialization } from "./trainingDraftStorage";

export type ParameterValue = null | boolean | number | string | ParameterValue[];
export type ParameterDraft =
  | { kind: "scalar"; text: string }
  | { kind: "array"; items: string[] }
  | { kind: "null" };
export type ParameterDrafts = Record<string, ParameterDraft>;
export type ParameterRule = {
  type?: string;
  enum?: unknown[];
  anyOf?: ParameterRule[];
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  multipleOf?: number;
  items?: ParameterRule;
  minItems?: number;
  maxItems?: number;
  ordered?: boolean;
  uniqueItems?: boolean;
};
export const CONFIGURATION_FILE_LIMIT = 64 * 1024;
export const commonParameterKeys = new Set([
  "epochs",
  "batch",
  "imgsz",
  "lr0",
  "patience",
  "seed",
  "optimizer",
  "weight_decay",
  "warmup_epochs",
  "mosaic",
  "mosaic_prob",
  "close_mosaic",
  "no_aug_epochs",
  "evaluate_train",
]);
const forbiddenKeys = new Set(["__proto__", "constructor", "prototype", "<<"]);
const numericText = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

export function valueToDraft(value: unknown): ParameterDraft {
  if (value === null) return { kind: "null" };
  if (Array.isArray(value)) return { kind: "array", items: value.map(String) };
  return { kind: "scalar", text: String(value) };
}

export function validateParameterValue(value: unknown, rule: ParameterRule): string | null {
  if (rule.anyOf) {
    return rule.anyOf.some((option) => !validateParameterValue(value, option))
      ? null
      : "类型或取值不符合允许形式";
  }
  if (rule.enum)
    return rule.enum.some((item) => item === value) ? null : `请选择 ${rule.enum.join(" / ")}`;
  switch (rule.type) {
    case "null":
      return value === null ? null : "请选择未设置";
    case "boolean":
      return typeof value === "boolean" ? null : "必须为布尔值";
    case "string":
      return typeof value === "string" ? null : "必须为文本";
    case "array": {
      if (!Array.isArray(value)) return "必须为列表";
      if (value.length < (rule.minItems ?? 0) || value.length > (rule.maxItems ?? 100))
        return "列表长度超出范围";
      if (!rule.items) return "当前列表类型尚不支持编辑";
      for (const item of value) {
        const error = validateParameterValue(item, rule.items);
        if (error) return error;
      }
      if (rule.uniqueItems && new Set(value).size !== value.length) return "列表元素不能重复";
      if (
        rule.ordered &&
        value.some((item, index) => index > 0 && Number(value[index - 1]) > Number(item))
      )
        return "区间下界不能大于上界";
      return null;
    }
    case "number":
    case "integer": {
      if (typeof value !== "number" || !Number.isFinite(value)) return "必须为有限数值";
      if (rule.type === "integer" && !Number.isSafeInteger(value)) return "必须为精确整数";
      if (rule.minimum !== undefined && value < rule.minimum) return `不能小于 ${rule.minimum}`;
      if (rule.exclusiveMinimum !== undefined && value <= rule.exclusiveMinimum)
        return `必须大于 ${rule.exclusiveMinimum}`;
      if (rule.maximum !== undefined && value > rule.maximum) return `不能大于 ${rule.maximum}`;
      if (rule.multipleOf && value % rule.multipleOf !== 0)
        return `必须为 ${rule.multipleOf} 的整数倍`;
      return null;
    }
    default:
      return "当前字段类型尚不支持编辑";
  }
}

export function ruleForDraft(rule: ParameterRule, draft: ParameterDraft): ParameterRule {
  if (!rule.anyOf) return rule;
  return (
    rule.anyOf.find((option) =>
      draft.kind === "null"
        ? option.type === "null"
        : draft.kind === "array"
          ? option.type === "array"
          : option.type !== "null" && option.type !== "array",
    ) ?? {}
  );
}

export function parseParameterDraft(
  field: TrainingParameterField,
  draft: ParameterDraft,
): { ok: true; value: ParameterValue } | { ok: false; error: string } {
  const rule = ruleForDraft(field.rules as ParameterRule, draft);
  let value: unknown;
  if (draft.kind === "null") value = null;
  else if (draft.kind === "array") {
    if (draft.items.some((item) => !numericText.test(item.trim())))
      return { ok: false, error: "请填写完整的数值列表" };
    value = draft.items.map((item) => Number(item));
  } else if (rule.enum || rule.type === "string") value = draft.text;
  else if (rule.type === "boolean")
    value = draft.text === "true" ? true : draft.text === "false" ? false : undefined;
  else {
    if (!numericText.test(draft.text.trim()))
      return { ok: false, error: "请填写有效数值，可使用科学记数法" };
    value = Number(draft.text);
  }
  const error = validateParameterValue(value, field.rules as ParameterRule);
  return error ? { ok: false, error } : { ok: true, value: value as ParameterValue };
}

export function collectOverrides(schema: TrainingParameterSchema, drafts: ParameterDrafts) {
  const fields = new Map(schema.fields.map((field) => [field.key, field]));
  const values: Record<string, ParameterValue> = {};
  const errors: Record<string, string> = {};
  for (const [key, draft] of Object.entries(drafts)) {
    const field = fields.get(key);
    if (!field || field.status !== "editable") {
      errors[key] = "该参数当前不可编辑";
      continue;
    }
    const parsed = parseParameterDraft(field, draft);
    if (parsed.ok) values[key] = parsed.value;
    else errors[key] = parsed.error;
  }
  return { values, errors };
}

export function describeRule(rule: ParameterRule): string {
  if (rule.anyOf) return rule.anyOf.map(describeRule).join("，或 ");
  if (rule.enum) return `可选：${rule.enum.join(" / ")}`;
  if (rule.type === "null") return "未设置";
  if (rule.type === "boolean") return "开启或关闭";
  if (rule.type === "array")
    return `${rule.minItems === rule.maxItems ? `${rule.minItems} 项` : "有界"}数值${rule.ordered ? "区间，下界不大于上界" : "列表"}`;
  const parts = [rule.type === "integer" ? "整数" : "数值"];
  if (rule.minimum !== undefined) parts.push(`≥ ${rule.minimum}`);
  if (rule.exclusiveMinimum !== undefined) parts.push(`> ${rule.exclusiveMinimum}`);
  if (rule.maximum !== undefined) parts.push(`≤ ${rule.maximum}`);
  if (rule.multipleOf) parts.push(`${rule.multipleOf} 的整数倍`);
  return parts.join(" · ");
}

export function portableConfiguration(
  schema: TrainingParameterSchema,
  baseModel: string,
  imageDigest: string,
  values: Record<string, ParameterValue>,
  initialization?: TrainingInitialization,
): string {
  return stringify(
    {
      format: "aap-training-configuration",
      version: initialization?.kind === "trained_model" ? 2 : 1,
      framework: schema.framework,
      framework_version: schema.framework_version,
      base_model: baseModel,
      schema_id: schema.schema_id,
      schema_digest: schema.schema_digest,
      image_digest: imageDigest,
      values,
      ...(initialization?.kind === "trained_model" ? { initialization } : {}),
    },
    { aliasDuplicateObjects: false, lineWidth: 0 },
  );
}

function plainRecord(value: unknown): value is Record<string, unknown> {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function assertJsonData(value: unknown, depth = 0): void {
  if (depth > 8) throw new Error("配置嵌套不能超过 8 层");
  if (typeof value === "number" && !Number.isFinite(value))
    throw new Error("配置不能包含非有限数值");
  if (value === null || ["string", "boolean", "number"].includes(typeof value)) return;
  if (Array.isArray(value)) {
    if (value.length > 150) throw new Error("配置列表过长");
    value.forEach((item) => assertJsonData(item, depth + 1));
    return;
  }
  if (!plainRecord(value) || Object.keys(value).length > 150)
    throw new Error("配置必须为有界的纯数据对象");
  for (const [key, item] of Object.entries(value)) {
    if (forbiddenKeys.has(key)) throw new Error(`不允许配置键 ${key}`);
    assertJsonData(item, depth + 1);
  }
}

export function importConfiguration(
  source: string,
  schema: TrainingParameterSchema,
  baseModel: string,
  imageDigest: string,
): {
  values: Record<string, ParameterValue>;
  errors: string[];
  warnings: string[];
  initialization?: TrainingInitialization;
} {
  const result = {
    values: {} as Record<string, ParameterValue>,
    errors: [] as string[],
    warnings: [] as string[],
    initialization: undefined as TrainingInitialization | undefined,
  };
  try {
    if (!source.trim() || new TextEncoder().encode(source).length > CONFIGURATION_FILE_LIMIT)
      throw new Error("请选择非空且不超过 64 KiB 的 YAML 文件");
    const documents = parseAllDocuments(source, {
      schema: "core",
      uniqueKeys: true,
      strict: true,
      version: "1.2",
      logLevel: "silent",
    });
    if (documents.length !== 1) throw new Error("配置必须只有一个 YAML 文档");
    const document = documents[0];
    if (document.errors.length || document.warnings.length)
      throw new Error(
        `YAML 格式错误：${[...document.errors, ...document.warnings][0].message.slice(0, 400)}`,
      );
    visit(document, (_key, node, path) => {
      if (path.length > 16) throw new Error("配置嵌套过深");
      if (isAlias(node) || (isNode(node) && (node.tag || ("anchor" in node && node.anchor))))
        throw new Error("配置不支持 YAML 标签、锚点或引用");
      if (isMap(node))
        for (const pair of node.items) {
          if (!isScalar(pair.key) || typeof pair.key.value !== "string")
            throw new Error("配置键必须为文本");
          if (forbiddenKeys.has(pair.key.value)) throw new Error(`不允许配置键 ${pair.key.value}`);
        }
    });
    const data: unknown = document.toJS({ maxAliasCount: 0 });
    assertJsonData(data);
    if (!plainRecord(data)) throw new Error("配置顶层必须为参数对象");
    let values = data;
    if ("format" in data) {
      const allowed = new Set([
        "format",
        "version",
        "framework",
        "framework_version",
        "base_model",
        "schema_id",
        "schema_digest",
        "image_digest",
        "values",
        ...(data.version === 2 ? ["initialization"] : []),
      ]);
      if (Object.keys(data).some((key) => !allowed.has(key)))
        throw new Error("可移植配置包含未支持的顶层字段");
      if (
        data.format !== "aap-training-configuration" ||
        (data.version !== 1 && data.version !== 2)
      )
        throw new Error("配置文件格式或格式版本不支持");
      if (data.version === 2) {
        if (
          !validInitialization(data.initialization) ||
          data.initialization.kind !== "trained_model" ||
          !data.initialization.source_digest
        )
          throw new Error("配置文件缺少完整的项目模型初始化身份");
        result.initialization = data.initialization;
      }
      if (data.framework !== schema.framework || data.base_model !== baseModel)
        throw new Error("请先切换到文件指定的训练器与基础权重，再导入");
      if (
        data.framework_version !== schema.framework_version ||
        data.schema_id !== schema.schema_id ||
        data.schema_digest !== schema.schema_digest
      )
        throw new Error("文件的参数定义与当前目录不同，请重新导出或手动核对");
      if (typeof data.image_digest !== "string" || !/^sha256:[0-9a-f]{64}$/.test(data.image_digest))
        throw new Error("文件缺少有效的运行环境身份");
      if (data.image_digest !== imageDigest)
        result.warnings.push("文件来自其他训练镜像；开始前会按当前受控环境重新检查。");
      if (!plainRecord(data.values)) throw new Error("values 必须为参数对象");
      values = data.values;
    }
    const fields = new Map(
      schema.fields
        .filter((field) => field.status === "editable")
        .map((field) => [field.key, field]),
    );
    const aliases = new Map(
      schema.fields.flatMap((field) => field.aliases.map((alias) => [alias, field.key] as const)),
    );
    const seen = new Set<string>();
    for (const [supplied, value] of Object.entries(values)) {
      const key = aliases.get(supplied) ?? supplied;
      const field = fields.get(key);
      if (!field) {
        result.errors.push(`${supplied}：未知、平台管理或当前不可编辑`);
        continue;
      }
      if (seen.has(key)) {
        result.errors.push(`${key}：别名与原生键不能同时提交`);
        continue;
      }
      seen.add(key);
      const error = validateParameterValue(value, field.rules as ParameterRule);
      if (error) result.errors.push(`${field.label}（${key}）：${error}`);
      else result.values[key] = value as ParameterValue;
    }
  } catch (error) {
    result.errors.push(error instanceof Error ? error.message : "无法读取配置文件");
  }
  return result;
}

export function compatibleDrafts(schema: TrainingParameterSchema, drafts: ParameterDrafts) {
  const fields = new Map(
    schema.fields.filter((field) => field.status === "editable").map((field) => [field.key, field]),
  );
  const retained: ParameterDrafts = {};
  const discarded: string[] = [];
  for (const [key, value] of Object.entries(drafts)) {
    const field = fields.get(key);
    if (field && parseParameterDraft(field, value).ok) retained[key] = value;
    else discarded.push(key);
  }
  return { retained, discarded };
}
