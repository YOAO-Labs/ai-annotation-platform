import { useMemo } from "react";
import type { AttributeSchema } from "@/api/projects";
import { AttributeForm } from "../shell/AttributeForm";

export interface VideoAttributesEditorProps {
  schema: AttributeSchema | undefined;
  className: string;
  trackAttributes: Record<string, unknown> | undefined;
  keyframeAttributes: Record<string, unknown> | undefined;
  frameIndex: number;
  canEditKeyframe: boolean;
  readOnly?: boolean;
  onChangeTrackAttributes: (next: Record<string, unknown>) => void;
  onChangeKeyframeAttributes: (next: Record<string, unknown>) => void;
}

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
