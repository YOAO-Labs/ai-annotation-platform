import { useMemo } from "react";
import type { AttributeSchema } from "@/api/projects";
import { AttributeForm } from "../shell/AttributeForm";

export interface VideoAttributesEditorProps {
  /** 当前激活工具的属性 schema (由 WorkbenchShell 派生)。 */
  schema: AttributeSchema | undefined;
  className: string;
  /** track 级默认值 (annotation.attributes)。 */
  trackAttributes: Record<string, unknown> | undefined;
  /** 当前帧逐帧覆盖 (keyframe.attributes); 为空表示该帧用 track 默认值。 */
  keyframeAttributes: Record<string, unknown> | undefined;
  frameIndex: number;
  /** 当前帧是否有可写关键帧 (消失帧不可写逐帧属性)。 */
  canEditKeyframe: boolean;
  readOnly?: boolean;
  onChangeTrackAttributes: (next: Record<string, unknown>) => void;
  onChangeKeyframeAttributes: (next: Record<string, unknown>) => void;
}

/**
 * 视频轨迹属性编辑器。
 *
 * 仅暴露 schema 中 `mutable=true` 的属性, 区分两层:
 * - 「track 默认值」写入 annotation.attributes (整条轨迹生效);
 * - 「当前帧覆盖」写入当前帧 keyframe.attributes (仅该帧覆盖默认值)。
 *
 * 复用既有 AttributeForm 渲染表单控件 (context="video" 会给字段挂「逐帧」徽标),
 * 通过裁剪 schema.fields 只保留 mutable 字段, 避免重复造控件。
 */
export function VideoAttributesEditor({
  schema,
  className,
  trackAttributes,
  keyframeAttributes,
  frameIndex,
  canEditKeyframe,
  readOnly,
  onChangeTrackAttributes,
  onChangeKeyframeAttributes,
}: VideoAttributesEditorProps) {
  // 仅保留 mutable=true 的字段; 非 mutable 属性在 track 创建后不应逐帧/逐 track 改动。
  const mutableSchema = useMemo<AttributeSchema | undefined>(() => {
    const fields = (schema?.fields ?? []).filter((f) => f.mutable === true);
    if (fields.length === 0) return undefined;
    return { fields };
  }, [schema]);

  if (!mutableSchema) return null;

  const overrideKeys = Object.keys(keyframeAttributes ?? {});

  return (
    <div
      className="grid gap-2.5 p-2 px-2.5 border border-border rounded-lg bg-card"
      data-testid="video-attributes-editor"
    >
      <div className="text-sm font-semibold">可变属性</div>

      <section className="grid gap-1">
        <div className="flex items-baseline justify-between gap-2 text-xs font-semibold text-foreground">
          轨迹默认值
          <span className="text-xs font-normal text-muted-foreground">整条轨迹生效</span>
        </div>
        <AttributeForm
          schema={mutableSchema}
          className={className}
          attributes={trackAttributes}
          onChange={onChangeTrackAttributes}
          readOnly={readOnly}
          context="video"
        />
      </section>

      <section className="grid gap-1">
        <div className="flex items-baseline justify-between gap-2 text-xs font-semibold text-foreground">
          当前帧覆盖
          <span className="text-xs font-normal text-muted-foreground">仅 F{frameIndex} 生效</span>
        </div>
        {canEditKeyframe ? (
          <>
            <AttributeForm
              schema={mutableSchema}
              className={className}
              attributes={keyframeAttributes}
              onChange={onChangeKeyframeAttributes}
              readOnly={readOnly}
              context="video"
            />
            <div className="text-xs text-muted-foreground">
              {overrideKeys.length > 0
                ? `已覆盖 ${overrideKeys.length} 项 · 留空回落到轨迹默认值`
                : "未覆盖 · 当前帧沿用轨迹默认值"}
            </div>
          </>
        ) : (
          <div className="text-xs text-muted-foreground">当前帧无关键帧, 无法设置逐帧覆盖</div>
        )}
      </section>
    </div>
  );
}
