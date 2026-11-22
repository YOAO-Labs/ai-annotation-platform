import { useEffect, useMemo, useState } from "react";
import { clsx } from "clsx";
import { useNavigate } from "react-router-dom";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useCreateProject } from "@/hooks/useProjects";
import {
  TOOL_UNIT_GROUPS,
  PROJECT_DATA_TYPES,
  defaultEnabledUnits,
  dataTypeFromLegacy,
  toolUnitFromLegacy,
  type ToolUnitId,
  type ProjectDataType,
} from "@/constants/toolUnits";
import {
  projectsApi,
  type ProjectResponse,
  type ClassesConfig,
  type AttributeField,
  type AttributeSchema,
  type ToolBindings,
} from "@/api/projects";
import { projectTemplatesApi, type ProjectTemplateOut } from "@/api/projectTemplates";
import type { ClassConfigEntry } from "@/api/projects";
import { defaultColorFor, type ClassRow } from "@/pages/Projects/sections/ClassEditor";
import { validateAttributeFields } from "@/pages/Projects/sections/AttributeSchemaEditor";
import { Step1DataTypeAndTools } from "./steps/Step1DataTypeAndTools";
import { Step2ClassesPerUnit } from "./steps/Step2ClassesPerUnit";
import { Step3AttributesPerUnit } from "./steps/Step3AttributesPerUnit";
import { Step4Ai } from "./steps/Step4Ai";
import { Step5Datasets } from "./steps/Step5Datasets";
import { Step6Members } from "./steps/Step6Members";
import { Step7Success } from "./steps/Step7Success";
import styles from "./CreateProjectWizard.module.css";

type Step = 1 | 2 | 3 | 4 | 5 | 6 | 7;

interface Props {
  open: boolean;
  onClose: () => void;
  sourceProjectId?: string;
  templateId?: string;
}

export interface UnitBindingForm {
  enabled: boolean;
  classRows: ClassRow[];
  attributeFields: AttributeField[];
}

export type UnitBindingMap = Partial<Record<ToolUnitId, UnitBindingForm>>;

export interface FormState {
  name: string;
  dataType: ProjectDataType;
  typeKey: string;
  dueDate: string;
  unitBindings: UnitBindingMap;
  activeUnit: ToolUnitId;
  aiEnabled: boolean;
  mlBackendSourceId: string;
  sceneMode: boolean;
  datasetIds: string[];
  splitStrategy: "none" | "random" | "by_scene";
  splitNBatches: number;
  members: { userId: string; role: "annotator" | "reviewer" }[];
  copyAnnotationGuide: boolean;
}

export function defaultUnitBindings(typeKey: string): UnitBindingMap {
  const dt = dataTypeFromLegacy(typeKey);
  const enabled = new Set(defaultEnabledUnits(dt));
  const out: UnitBindingMap = {};
  for (const g of TOOL_UNIT_GROUPS) {
    if (!g.available) continue;
    if (!g.dataTypes.includes(dt)) continue;
    out[g.id] = {
      enabled: enabled.has(g.id),
      classRows: [],
      attributeFields: [],
    };
  }
  return out;
}

const INITIAL: FormState = {
  name: "",
  dataType: "image",
  typeKey: "image-det",
  dueDate: "",
  unitBindings: defaultUnitBindings("image-det"),
  activeUnit: "bbox",
  aiEnabled: false,
  mlBackendSourceId: "",
  sceneMode: false,
  datasetIds: [],
  splitStrategy: "none",
  splitNBatches: 0,
  members: [],
  copyAnnotationGuide: true,
};

type StepperStep = 1 | 2 | 3 | 4 | 5 | 6;

const STEP_LABELS: Record<StepperStep, string> = {
  1: "类型",
  2: "类别",
  3: "属性",
  4: "AI 接入",
  5: "数据",
  6: "成员",
};

const DRAFT_KEY = "create_project_draft_v0_14_4";

function buildUnitBindingsFromSource(src: {
  type_key?: string;
  classes?: string[];
  classes_config?: Record<string, unknown> | null;
  attribute_schema?: AttributeSchema | null;
  tool_bindings?: ToolBindings | Record<string, unknown> | null;
}): { unitBindings: UnitBindingMap; activeUnit: ToolUnitId } {
  const typeKey = src.type_key ?? "image-det";
  const base = defaultUnitBindings(typeKey);
  const tb = (src.tool_bindings ?? {}) as ToolBindings;

  for (const unitId of Object.keys(tb) as ToolUnitId[]) {
    const binding = tb[unitId];
    if (!binding) continue;
    const classRows: ClassRow[] = (binding.classes ?? [])
      .slice()
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
      .map((c) => ({
        name: c.name,
        color: c.color ?? defaultColorFor(c.name),
        ...(c.alias ? { alias: c.alias } : {}),
      }));
    const attributeFields: AttributeField[] =
      (binding.attribute_schema as AttributeSchema | undefined)?.fields ?? [];
    base[unitId] = {
      enabled: !!binding.enabled,
      classRows,
      attributeFields,
    };
  }

  const hasTb = Object.keys(tb).length > 0;
  if (!hasTb) {
    const defaultUnit = toolUnitFromLegacy(typeKey);
    const cfg = (src.classes_config ?? {}) as ClassesConfig;
    const ordered = [...(src.classes ?? [])].sort((a, b) => {
      const oa = cfg[a]?.order ?? 0;
      const ob = cfg[b]?.order ?? 0;
      return oa - ob;
    });
    const classRows = ordered.map((name) => {
      const c: ClassConfigEntry | undefined = cfg[name];
      return {
        name,
        color: c?.color ?? defaultColorFor(name),
        ...(c?.alias ? { alias: c.alias } : {}),
      } as ClassRow;
    });
    base[defaultUnit] = {
      enabled: true,
      classRows,
      attributeFields: (src.attribute_schema as AttributeSchema | undefined)?.fields ?? [],
    };
  }

  const firstEnabled = (Object.keys(base) as ToolUnitId[]).find((k) => base[k]?.enabled);
  return { unitBindings: base, activeUnit: firstEnabled ?? "bbox" };
}

function buildFormFromSource(src: ProjectResponse): FormState {
  const { unitBindings, activeUnit } = buildUnitBindingsFromSource(src);
  return {
    ...INITIAL,
    name: src.name ? `${src.name} (副本)` : "",
    dataType: src.data_type ? (src.data_type as ProjectDataType) : dataTypeFromLegacy(src.type_key),
    typeKey: src.type_key,
    unitBindings,
    activeUnit,
    aiEnabled: src.ai_enabled,
    mlBackendSourceId: src.ml_backend_id ?? "",
    sceneMode: !!src.scene_mode,
    splitStrategy: src.scene_mode ? "by_scene" : "none",
  };
}

function buildFormFromTemplate(t: ProjectTemplateOut): FormState {
  const { unitBindings, activeUnit } = buildUnitBindingsFromSource(
    t as Parameters<typeof buildUnitBindingsFromSource>[0],
  );
  return {
    ...INITIAL,
    name: "",
    dataType: t.data_type ? (t.data_type as ProjectDataType) : dataTypeFromLegacy(t.type_key),
    typeKey: t.type_key,
    unitBindings,
    activeUnit,
    aiEnabled: t.ai_enabled,
    sceneMode: false,
  };
}

export function CreateProjectWizard({ open, onClose, sourceProjectId, templateId }: Props) {
  const navigate = useNavigate();
  const pushToast = useToastStore((s) => s.push);
  const createProject = useCreateProject();

  const [step, setStep] = useState<Step>(1);
  const [form, setForm] = useState<FormState>(INITIAL);
  const [created, setCreated] = useState<ProjectResponse | null>(null);
  const [prefilling, setPrefilling] = useState(false);

  useEffect(() => {
    if (!open) {
      setStep(1);
      setForm(INITIAL);
      setCreated(null);
      setPrefilling(false);
      createProject.reset();
      return;
    }
    if (sourceProjectId) {
      let cancelled = false;
      setPrefilling(true);
      projectsApi
        .get(sourceProjectId)
        .then((src) => {
          if (cancelled) return;
          setForm(buildFormFromSource(src));
        })
        .catch((e) => {
          if (cancelled) return;
          pushToast({
            msg: "源项目读取失败, 走普通新建",
            sub: (e as Error)?.message ?? "请稍后重试",
            kind: "error",
          });
        })
        .finally(() => {
          if (!cancelled) setPrefilling(false);
        });
      return () => {
        cancelled = true;
      };
    }
    if (templateId) {
      let cancelled = false;
      setPrefilling(true);
      projectTemplatesApi
        .get(templateId)
        .then((t) => {
          if (cancelled) return;
          setForm(buildFormFromTemplate(t));
        })
        .catch((e) => {
          if (cancelled) return;
          pushToast({
            msg: "模板读取失败, 走普通新建",
            sub: (e as Error)?.message ?? "请稍后重试",
            kind: "error",
          });
        })
        .finally(() => {
          if (!cancelled) setPrefilling(false);
        });
      return () => {
        cancelled = true;
      };
    }
    try {
      const saved = localStorage.getItem(DRAFT_KEY);
      if (saved) setForm({ ...INITIAL, ...JSON.parse(saved) });
    } catch {
      // ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sourceProjectId, templateId]);

  useEffect(() => {
    if (open && !created && !sourceProjectId && !templateId) {
      try {
        localStorage.setItem(DRAFT_KEY, JSON.stringify(form));
      } catch {
        /* */
      }
    }
  }, [form, open, created, sourceProjectId, templateId]);

  const selectedDataType = useMemo(
    () => PROJECT_DATA_TYPES.find((t) => t.id === form.dataType) ?? PROJECT_DATA_TYPES[0],
    [form.dataType],
  );

  const trimmedName = form.name.trim();
  const nameValid = trimmedName.length >= 2 && trimmedName.length <= 60;
  const dueValid = !form.dueDate || form.dueDate >= new Date().toISOString().slice(0, 10);
  const enabledUnitCount = (Object.keys(form.unitBindings) as ToolUnitId[]).filter(
    (k) => form.unitBindings[k]?.enabled,
  ).length;
  const step1Valid = nameValid && !!form.dataType && dueValid && enabledUnitCount > 0;

  const enabledUnitIds = useMemo(
    () =>
      (Object.keys(form.unitBindings) as ToolUnitId[]).filter((k) => form.unitBindings[k]?.enabled),
    [form.unitBindings],
  );
  const step3AttrError = useMemo<string | null>(() => {
    for (const unitId of enabledUnitIds) {
      const fields = form.unitBindings[unitId]?.attributeFields ?? [];
      if (fields.length === 0) continue;
      const err = validateAttributeFields(fields);
      if (err) return `[${unitId}] ${err}`;
    }
    return null;
  }, [enabledUnitIds, form.unitBindings]);
  const step3Valid = step3AttrError === null;

  const submit = () => {
    if (step3AttrError) {
      pushToast({ msg: step3AttrError, kind: "error" });
      return;
    }
    const tool_bindings: ToolBindings = {};
    for (const unitId of enabledUnitIds) {
      const ub = form.unitBindings[unitId];
      if (!ub) continue;
      tool_bindings[unitId] = {
        enabled: true,
        classes: ub.classRows.map((r, i) => ({
          name: r.name,
          color: r.color,
          order: i,
          ...(r.alias ? { alias: r.alias } : {}),
        })),
        attribute_schema:
          ub.attributeFields.length > 0 ? { fields: ub.attributeFields } : { fields: [] },
      };
    }
    const activeRows = form.unitBindings[form.activeUnit]?.classRows ?? [];
    const activeFields = form.unitBindings[form.activeUnit]?.attributeFields ?? [];
    const classes = activeRows.map((r) => r.name);
    const classes_config: ClassesConfig = {};
    activeRows.forEach((r, i) => {
      classes_config[r.name] = {
        color: r.color,
        order: i,
        ...(r.alias ? { alias: r.alias } : {}),
      };
    });
    const attribute_schema: AttributeSchema | undefined =
      activeFields.length > 0 ? { fields: activeFields } : undefined;
    createProject.mutate(
      {
        name: trimmedName,
        data_type: selectedDataType.id,
        type_key: selectedDataType.legacyTypeKey,
        type_label: selectedDataType.label,
        classes,
        classes_config,
        attribute_schema,
        tool_bindings,
        ai_enabled: form.aiEnabled,
        ml_backend_source_id:
          form.aiEnabled && form.mlBackendSourceId ? form.mlBackendSourceId : null,
        scene_mode: form.sceneMode,
        source_project_id: sourceProjectId ?? null,
        copy_annotation_guide: sourceProjectId ? form.copyAnnotationGuide : undefined,
        template_id: templateId ?? null,
        due_date: form.dueDate || null,
      },
      {
        onSuccess: async (p) => {
          setCreated(p);
          pushToast({ msg: "项目创建成功", sub: p.display_id, kind: "success" });
          setStep(5);
        },
        onError: (err) => {
          pushToast({
            msg: "创建失败",
            sub: (err as Error)?.message ?? "请稍后重试",
          });
        },
      },
    );
  };

  const finishWizard = (_linkedDatasets: number, _addedMembers: number) => {
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      /* */
    }
    setStep(7);
  };

  const stepperCurrent = (step >= 1 && step <= 6 ? step : 6) as StepperStep;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        step === 7
          ? "创建完成"
          : templateId
            ? "从模板创建项目"
            : sourceProjectId
              ? "复制项目配置"
              : "新建项目"
      }
      width={620}
    >
      <div className={styles.wizard} data-testid="project-wizard">
        {step !== 7 && <Stepper current={stepperCurrent} />}
        {templateId && !sourceProjectId && step !== 7 && (
          <div className={styles.copyBanner}>
            <Icon name="book" size={12} />
            {prefilling
              ? "正在从模板加载配置…"
              : "已用模板字段预填表单, 提交后将复制到新项目 (模板的 annotation_guide 也会一并应用; guide_assets 不携带)"}
          </div>
        )}
        {sourceProjectId && step !== 7 && (
          <div className={styles.copyBanner}>
            <Icon name="copy" size={12} />
            {prefilling
              ? "正在从源项目加载配置…"
              : "已用源项目配置预填表单, 提交后将复制到新项目 (不复制数据集 / 任务 / 成员)"}
            {!prefilling && (
              <label className={styles.copyGuideToggle}>
                <input
                  type="checkbox"
                  checked={form.copyAnnotationGuide}
                  onChange={(e) =>
                    setForm((s) => ({ ...s, copyAnnotationGuide: e.target.checked }))
                  }
                />
                同时复制标注指引（共享图片引用，独立删除）
              </label>
            )}
          </div>
        )}

        <div className={clsx(styles.stepContent, step === 2 && styles.classesContent)}>
          {step === 1 && (
            <Step1DataTypeAndTools
              form={form}
              setForm={setForm}
              nameValid={nameValid || trimmedName.length === 0}
              dueValid={dueValid}
            />
          )}

          {step === 2 && <Step2ClassesPerUnit form={form} setForm={setForm} />}

          {step === 3 && (
            <Step3AttributesPerUnit form={form} setForm={setForm} error={step3AttrError} />
          )}

          {step === 4 && <Step4Ai form={form} setForm={setForm} />}

          {step === 5 && created && (
            <Step5Datasets
              project={created}
              form={form}
              setForm={setForm}
              onNext={(linked) => {
                void linked;
                setStep(6);
              }}
            />
          )}

          {step === 6 && created && (
            <Step6Members
              project={created}
              form={form}
              setForm={setForm}
              onNext={(added) => {
                finishWizard(form.datasetIds.length, added);
              }}
            />
          )}

          {step === 7 && created && (
            <Step7Success
              project={created}
              summary={{
                datasets: form.datasetIds.length,
                members: form.members.length,
              }}
              onOpenProject={() => {
                onClose();
                navigate(`/projects/${created.id}/annotate`);
              }}
              onOpenSettings={() => {
                onClose();
                navigate(`/projects/${created.id}/settings`);
              }}
              onOpenReadiness={() => {
                onClose();
                navigate(`/projects/${created.id}/settings?section=readiness`);
              }}
              onDone={onClose}
            />
          )}
        </div>

        {(step === 1 || step === 2 || step === 3 || step === 4) && (
          <Footer
            step={step}
            canNext={
              (step === 1 && step1Valid) || step === 2 || (step === 3 && step3Valid) || step === 4
            }
            loading={createProject.isPending}
            onCancel={onClose}
            onPrev={() => setStep((s) => (s > 1 ? ((s - 1) as Step) : s))}
            onNext={() => {
              if (step === 1) setStep(2);
              else if (step === 2) setStep(3);
              else if (step === 3) setStep(4);
              else submit();
            }}
          />
        )}
      </div>
    </Modal>
  );
}

function Stepper({ current }: { current: StepperStep }) {
  const steps: StepperStep[] = [1, 2, 3, 4, 5, 6];
  const lastIdx = steps.length - 1;
  return (
    <div className={styles.stepper}>
      {steps.map((n, i) => {
        const active = n === current;
        const done = n < current;
        return (
          <div key={n} className={clsx(styles.stepperItem, i < lastIdx && styles.stepperItemGrow)}>
            <div
              className={clsx(
                styles.stepperDot,
                (done || active) && styles.stepperDotActive,
                active && styles.stepperDotCurrent,
              )}
            >
              {done ? <Icon name="check" size={11} /> : n}
            </div>
            <span
              className={clsx(
                styles.stepperLabel,
                (done || active) && styles.stepperLabelActive,
                active && styles.stepperLabelCurrent,
              )}
            >
              {STEP_LABELS[n]}
            </span>
            {i < lastIdx && (
              <div className={clsx(styles.stepperLine, n < current && styles.stepperLineDone)} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function Footer({
  step,
  canNext,
  loading,
  onCancel,
  onPrev,
  onNext,
}: {
  step: 1 | 2 | 3 | 4;
  canNext: boolean;
  loading: boolean;
  onCancel: () => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const isFinalEditStep = step === 4;
  return (
    <div className={styles.footer}>
      <Button variant="ghost" onClick={onCancel}>
        取消
      </Button>
      {step > 1 && (
        <Button onClick={onPrev}>
          <Icon name="chevLeft" size={12} />
          上一步
        </Button>
      )}
      <Button variant="primary" onClick={onNext} disabled={!canNext || loading}>
        {isFinalEditStep ? (loading ? "创建中..." : "创建") : "下一步"}
        {!isFinalEditStep && <Icon name="chevRight" size={12} />}
      </Button>
    </div>
  );
}
