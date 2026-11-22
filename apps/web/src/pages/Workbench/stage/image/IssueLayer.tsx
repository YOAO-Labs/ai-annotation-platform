import { useMemo } from "react";
import { Circle, Layer, Rect, Text } from "react-konva";
import type Konva from "konva";

import { hasPixelAnchor, type AnnotationFeedback } from "@/api/feedbacks";
import { useTheme } from "@/hooks/useTheme";
import { cssVarToHex } from "../colors";
import {
  ISSUE_PIN_HIGHLIGHT_RING_PX,
  ISSUE_PIN_RADIUS_PX,
  ISSUE_PIN_SELECTED_STROKE_PX,
  ISSUE_PIN_STROKE_PX,
  ISSUE_PIN_SYMBOL_PX,
  issuePinAriaLabel,
  issuePinColorVar,
  issuePinSymbol,
} from "../issuePinVisuals";

interface Props {
  pixelIssues: AnnotationFeedback[];
  imgW: number;
  imgH: number;
  scale: number;
  highlightId?: string | null;
  onPinClick?: (id: string) => void;
  armedForDrop?: boolean;
  onDrop?: (x: number, y: number) => void;
}

export function IssueLayer({
  pixelIssues,
  imgW,
  imgH,
  scale,
  highlightId,
  onPinClick,
  armedForDrop = false,
  onDrop,
}: Props) {
  const { resolved: theme } = useTheme();
  const safeScale = Math.max(scale, 0.0001);
  const colors = useMemo(() => {
    const vars = [
      "--sc-card",
      "--sc-brand",
      "--sc-status-info-alt",
      "--sc-status-caution",
      "--sc-status-danger",
      "--sc-status-positive",
      "--sc-muted-foreground",
    ];
    return Object.fromEntries(vars.map((name) => [name, cssVarToHex(name, theme)]));
  }, [theme]);
  const pinR = ISSUE_PIN_RADIUS_PX / safeScale;
  const pinStroke = ISSUE_PIN_STROKE_PX / safeScale;
  const selectedStroke = ISSUE_PIN_SELECTED_STROKE_PX / safeScale;
  const highlightRing = ISSUE_PIN_HIGHLIGHT_RING_PX / safeScale;
  const textSize = ISSUE_PIN_SYMBOL_PX / safeScale;
  const ringColor = colors["--sc-card"];
  const highlightColor = colors["--sc-brand"];

  return (
    <Layer name="issue-pins" listening={true}>
      {pixelIssues.map((issue) => {
        if (!hasPixelAnchor(issue) || highlightId !== issue.id) return null;
        const x = issue.anchor_position.x * imgW;
        const y = issue.anchor_position.y * imgH;
        return (
          <Circle
            key={`ring-${issue.id}`}
            name={`issue-pin-ring-${issue.id}`}
            x={x}
            y={y}
            radius={pinR + highlightRing}
            stroke={highlightColor}
            strokeWidth={pinStroke}
            listening={false}
          />
        );
      })}
      {pixelIssues.map((issue) => {
        if (!hasPixelAnchor(issue)) return null;
        const x = issue.anchor_position.x * imgW;
        const y = issue.anchor_position.y * imgH;
        const color = colors[issuePinColorVar(issue.status, issue.severity)];
        const isHighlight = highlightId === issue.id;
        return (
          <Circle
            key={issue.id}
            name={`issue-pin-${issue.id}`}
            x={x}
            y={y}
            radius={pinR}
            fill={color}
            stroke={ringColor}
            strokeWidth={isHighlight ? selectedStroke : pinStroke}
            shadowEnabled={isHighlight}
            shadowBlur={6 / safeScale}
            shadowOpacity={0.8}
            shadowColor={color}
            listening={!!onPinClick}
            onPointerDown={(e) => {
              e.cancelBubble = true;
            }}
            onClick={(e: Konva.KonvaEventObject<MouseEvent>) => {
              e.cancelBubble = true;
              onPinClick?.(issue.id);
            }}
            aria-label={issuePinAriaLabel(issue)}
          />
        );
      })}
      {pixelIssues.map((issue) => {
        if (!hasPixelAnchor(issue)) return null;
        const x = issue.anchor_position.x * imgW;
        const y = issue.anchor_position.y * imgH;
        return (
          <Text
            key={`label-${issue.id}`}
            x={x - pinR}
            y={y - pinR}
            width={pinR * 2}
            height={pinR * 2}
            text={issuePinSymbol(issue.status, issue.severity)}
            fontSize={textSize}
            fontStyle="bold"
            fill={ringColor}
            align="center"
            verticalAlign="middle"
            listening={false}
          />
        );
      })}
      {armedForDrop && imgW > 0 && imgH > 0 && (
        <Rect
          x={0}
          y={0}
          width={imgW}
          height={imgH}
          fill="rgba(0,0,0,0)"
          listening={true}
          onMouseEnter={(e: Konva.KonvaEventObject<MouseEvent>) => {
            const stage = e.target.getStage();
            if (stage?.container()) stage.container().style.cursor = "crosshair";
          }}
          onMouseLeave={(e: Konva.KonvaEventObject<MouseEvent>) => {
            const stage = e.target.getStage();
            if (stage?.container()) stage.container().style.cursor = "";
          }}
          onClick={(e: Konva.KonvaEventObject<MouseEvent>) => {
            e.cancelBubble = true;
            const stage = e.target.getStage();
            if (!stage) return;
            const pointer = stage.getPointerPosition();
            if (!pointer) return;
            const transform = stage.getAbsoluteTransform().copy().invert();
            const local = transform.point(pointer);
            const x = local.x / imgW;
            const y = local.y / imgH;
            if (x < 0 || x > 1 || y < 0 || y > 1) return;
            onDrop?.(x, y);
          }}
        />
      )}
    </Layer>
  );
}
