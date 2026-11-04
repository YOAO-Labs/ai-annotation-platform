// v0.10.14 · E2 · 模板新建 / 编辑表单 (Modal).
// v0.10.17 · 加 tool_bindings 编辑能力 (基础 / 工具与类别 / 渲染配置 三 tab),
// 复用 ProjectSettings 的 ClassEditor / AttributeSchemaEditor / ToolUnitTabs.

import { lazy, Suspense, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { PROJECT_TYPES } from "@/constants/projectTypes";
import { usePermissions } from "@/hooks/usePermissions";
import { useCreateProjectTemplate, useUpdateProjectTemplate } from "@/hooks/useProjectTemplates";
import type { ProjectTemplateOut, TemplateScope } from "@/api/projectTemplates";
import type { AttributeField, ProjectRenderingConfig } from "@/api/projects";
import { ClassEditor, type ClassRow } from "@/pages/Projects/sections/ClassEditor";
import { AttributeSchemaEditor } from "@/pages/Projects/sections/AttributeSchemaEditor";
import { ToolUnitTabs } from "@/pages/Projects/sections/ToolUnitTabs";
import { RenderingConfigEditor } from "@/pages/Projects/sections/RenderingConfigEditor";
import {
  buildUnitBindings,
  unitBindingsToPayload,
  type UnitBindingMap,
} from "@/pages/Projects/sections/useProjectToolBindings";
import { dataTypeFromLegacy, type ToolUnitId } from "@/constants/toolUnits";
import { ANNOTATION_GUIDE_STARTER } from "@/components/markdown/markdownGuideStarter";

import styles from "./ProjectTemplatesPage.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  /** undefined = 新建; 给值 = 编辑. */
  initial?: ProjectTemplateOut;
}

type Tab = "basic" | "tools" | "rendering";

const MarkdownEditor = lazy(() =>
  import("@/components/markdown/MarkdownEditor").then((module) => ({
    default: module.MarkdownEditor,
  })),
);

export function TemplateEditModal({ open, onClose, initial }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const { role } = usePermissions();
  const isEdit = !!initial;
  const create = useCreateProjectTemplate();
  const update = useUpdateProjectTemplate(initial?.id ?? "");

  const [tab, setTab] = useState<Tab>("basic");
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [scope, setScope] = useState<TemplateScope>(initial?.scope ?? "private");
  const [typeKey, setTypeKey] = useState(initial?.type_key ?? PROJECT_TYPES[0].key);
  const [annotationGuide, setAnnotationGuide] = useState(initial?.annotation_guide ?? "");
  const [bindings, setBindings] = useState<UnitBindingMap>(() =>
    buildUnitBindings({
      type_key: initial?.type_key,
      classes: initial?.classes,
      classes_config: initial?.classes_config,
      attribute_schema: initial?.attribute_schema,
      tool_bindings: initial?.tool_bindings,
    }),
  );
  const [activeUnit, setActiveUnit] = useState<ToolUnitId>("bbox");
  const [renderingConfig, setRenderingConfig] = useState<ProjectRenderingConfig>(
    initial?.rendering_config ?? {},
  );

  useEffect(() => {
    setName(initial?.name ?? "");
    setDescription(initial?.description ?? "");
    setScope(initial?.scope ?? "private");
    setTypeKey(initial?.type_key ?? PROJECT_TYPES[0].key);
    setAnnotationGuide(initial?.annotation_guide ?? "");
    setBindings(
      buildUnitBindings({
        type_key: initial?.type_key,
        classes: initial?.classes,
        classes_config: initial?.classes_config,
        attribute_schema: initial?.attribute_schema,
        tool_bindings: initial?.tool_bindings,
      }),
    );
    setRenderingConfig(initial?.rendering_config ?? {});
    setTab("basic");
  }, [initial, open]);

  const typeLabel = useMemo(
    () => PROJECT_TYPES.find((t) => t.key === typeKey)?.label ?? typeKey,
    [typeKey],
  );

  const activeBinding = bindings[activeUnit];

  const onChangeClasses = (next: ClassRow[]) => {
    setBindings((b) => ({
      ...b,
      [activeUnit]: {
        enabled: b[activeUnit]?.enabled ?? true,
        classRows: next,
        attributeFields: b[activeUnit]?.attributeFields ?? [],
      },
    }));
  };

  const onChangeAttributes = (next: AttributeField[]) => {
    setBindings((b) => ({
      ...b,
      [activeUnit]: {
        enabled: b[activeUnit]?.enabled ?? true,
        classRows: b[activeUnit]?.classRows ?? [],
        attributeFields: next,
      },
    }));
  };

  const onToggleUnit = (unit: ToolUnitId, enabled: boolean) => {
    setBindings((b) => ({
      ...b,
      [unit]: {
        enabled,
        classRows: b[unit]?.classRows ?? [],
        attributeFields: b[unit]?.attributeFields ?? [],
      },
    }));
  };

  const canPickPublic = role === "super_admin";
  const submitting = create.isPending || update.isPending;

  const handleSubmit = () => {
    if (!name.trim()) {
      pushToast({ msg: "请填写模板名称", kind: "warning" });
      return;
    }
    const tool_bindings = unitBindingsToPayload(bindings);
    // v0.10.17 · 旧扁平 classes 派生自首个 enabled unit, 兼容旧 reader.
    const firstUnitClasses =
      (Object.keys(tool_bindings) as ToolUnitId[])
        .map((k) => tool_bindings[k]?.classes ?? [])
        .find((cs) => cs.length > 0) ?? [];
    const classes = firstUnitClasses.map((c) => c.name);

    const payload = {
      name: name.trim(),
      description: description.trim() || null,
      type_label: typeLabel,
      type_key: typeKey,
      classes,
      tool_bindings,
      // Preserve intentional Markdown whitespace. Empty or whitespace-only
      // input keeps the existing nullable template contract.
      annotation_guide: annotationGuide.trim() ? annotationGuide : null,
      scope,
      rendering_config: renderingConfig,
    };

    if (isEdit) {
      update.mutate(payload, {
        onSuccess: () => {
          pushToast({ msg: "已保存模板", kind: "success" });
          onClose();
        },
        onError: (err) =>
          pushToast({
            msg: err instanceof Error ? err.message : "保存失败",
            kind: "warning",
          }),
      });
    } else {
      create.mutate(payload, {
        onSuccess: () => {
          pushToast({ msg: "模板已创建", kind: "success" });
          onClose();
        },
        onError: (err) =>
          pushToast({
            msg: err instanceof Error ? err.message : "创建失败",
            kind: "warning",
          }),
      });
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={isEdit ? "编辑模板" : "新建模板"} width={680}>
      <div className={styles.modalBody}>
        <div className={styles.tabs}>
          {(["basic", "tools", "rendering"] as Tab[]).map((t) => (
            <button
              key={t}
              type="button"
              className={t === tab ? styles.tabActive : styles.tab}
              onClick={() => setTab(t)}
            >
              {t === "basic" ? "基础信息" : t === "tools" ? "工具与类别" : "渲染配置"}
            </button>
          ))}
        </div>

        {tab === "basic" && (
          <>
            <label className={styles.label}>
              名称
              <input
                className={styles.input}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如：道路场景标准模板"
              />
            </label>

            <label className={styles.label}>
              描述（可选）
              <textarea
                className={styles.textarea}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="模板用途、适用场景"
              />
            </label>

            <label className={styles.label}>
              项目类型
              <select
                className={styles.select}
                value={typeKey}
                onChange={(e) => setTypeKey(e.target.value)}
              >
                {PROJECT_TYPES.map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.label}
                  </option>
                ))}
              </select>
            </label>

            <div className={styles.label}>
              <span>标注指引（Markdown，可选）</span>
              <p className={styles.help}>
                模板只保存 Markdown
                文本；项目指引中的私有图片资源不会随模板复制，请在项目内重新上传。
              </p>
              <Suspense fallback={<div className={styles.help}>编辑器加载中…</div>}>
                <MarkdownEditor
                  key={initial?.id ?? "new-template"}
                  value={annotationGuide}
                  onChange={setAnnotationGuide}
                  documentId={initial?.id ?? "new-template"}
                  label="模板标注指引编辑器"
                  variant="document"
                  disabled={submitting}
                  placeholder="输入类别定义和标注规则…"
                />
              </Suspense>
              {!annotationGuide.trim() && (
                <button
                  type="button"
                  className={styles.tab}
                  onClick={() => setAnnotationGuide(ANNOTATION_GUIDE_STARTER)}
                  disabled={submitting}
                >
                  插入指引模板
                </button>
              )}
            </div>

            <label className={styles.label}>
              可见范围
              <select
                className={styles.select}
                value={scope}
                onChange={(e) => setScope(e.target.value as TemplateScope)}
              >
                <option value="private">私有（仅自己可见）</option>
                <option value="organization">组织（同组织可见）</option>
                <option value="public" disabled={!canPickPublic}>
                  公共（全平台可见，仅超管可建）
                </option>
              </select>
              {scope === "organization" ? (
                <span className={styles.warning}>
                  组织模板需在创建后通过 PATCH 指定 organization_id（首版未提供 UI 字段）
                </span>
              ) : null}
            </label>
          </>
        )}

        {tab === "tools" && (
          <>
            <p className={styles.help}>
              每个工具单位独立配置类别与属性。勾选启用工具单位后在对应标签页编辑；不同工具的同名类别是独立记录。
            </p>
            <ToolUnitTabs
              bindings={bindings}
              activeUnit={activeUnit}
              onSelect={setActiveUnit}
              dataType={dataTypeFromLegacy(typeKey)}
              allowToggle
              onToggle={onToggleUnit}
            />
            {!activeBinding?.enabled ? (
              <p className={styles.help}>
                当前工具单位未启用 — 勾选上方复选框以启用并配置类别 / 属性。
              </p>
            ) : (
              <>
                <h4 className={styles.subheading}>类别</h4>
                <ClassEditor value={activeBinding.classRows} onChange={onChangeClasses} max={50} />
                <h4 className={styles.subheading}>属性配置</h4>
                <AttributeSchemaEditor
                  value={activeBinding.attributeFields}
                  onChange={onChangeAttributes}
                />
              </>
            )}
          </>
        )}

        {tab === "rendering" && (
          <>
            <p className={styles.help}>
              项目级渲染配置覆盖（snapToGrid / smoothImage / cssImageFilter / controlPointsSize）。
              覆盖关闭时该字段不下发，使用成员个人「标注偏好」。
            </p>
            <RenderingConfigEditor
              value={renderingConfig}
              onChange={setRenderingConfig}
              disabled={submitting}
            />
          </>
        )}

        <div className={styles.modalActions}>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button variant="primary" onClick={handleSubmit} disabled={submitting}>
            {submitting ? "保存中…" : isEdit ? "保存" : "创建"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
