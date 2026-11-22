import { useMemo } from "react";
import { Layer, Circle, Text, Rect } from "react-konva";
import type Konva from "konva";
import { useTheme } from "@/hooks/useTheme";
import { cssVarToHex } from "./colors";
import type { VideoPixelSize } from "./videoKonvaCoordinates";
import { hasPixelAnchor, type AnnotationFeedback } from "@/api/feedbacks";
import {
  ISSUE_PIN_HIGHLIGHT_RING_PX,
  ISSUE_PIN_RADIUS_PX,
  ISSUE_PIN_SELECTED_STROKE_PX,
  ISSUE_PIN_STROKE_PX,
  ISSUE_PIN_SYMBOL_PX,
  issuePinAriaLabel,
  issuePinColorVar,
  issuePinSymbol,
} from "./issuePinVisuals";

interface VideoKonvaIssueLayerProps {
  pixelIssues: AnnotationFeedback[];
  frameIndex: number;
  size: VideoPixelSize;
  scale: number;
  highlightId?: string | null;
  onPinClick?: (id: string) => void;
  dropArmed?: boolean;
  onDrop?: (x: number, y: number, frame: number) => void;
}

export function VideoKonvaIssueLayer({
  pixelIssues,
  frameIndex,
  size,
  scale,
  highlightId,
  onPinClick,
  dropArmed = false,
  onDrop,
}: VideoKonvaIssueLayerProps) {
  const { resolved: theme } = useTheme();
  const ringColor = useMemo(() => cssVarToHex("--sc-card", theme), [theme]);
  const pinColors = useMemo(() => {
    const fills: Record<string, string> = {};
    for (const varName of [
      "--sc-brand",
      "--sc-status-info-alt",
      "--sc-status-caution",
      "--sc-status-danger",
      "--sc-status-positive",
      "--sc-muted-foreground",
    ]) {
      fills[varName] = cssVarToHex(varName, theme);
    }
    return fills;
  }, [theme]);
  const onFrame = pixelIssues
    .filter(hasPixelAnchor)
    .filter((issue) => issue.anchor_position.frame === frameIndex);
  if (onFrame.length === 0 && !dropArmed) return null;
  const safeScale = Math.max(scale, 0.0001);
  const radius = ISSUE_PIN_RADIUS_PX / safeScale;
  const pinStroke = ISSUE_PIN_STROKE_PX / safeScale;
  const selectedStroke = ISSUE_PIN_SELECTED_STROKE_PX / safeScale;
  const highlightRing = ISSUE_PIN_HIGHLIGHT_RING_PX / safeScale;
  const symbolSize = ISSUE_PIN_SYMBOL_PX / safeScale;
  const highlightColor = pinColors["--sc-brand"];
  const clickable = !!onPinClick;
  const setCursor = (e: Konva.KonvaEventObject<MouseEvent>, cursor: string) => {
    const stage = e.target.getStage();
    if (stage) stage.container().style.cursor = cursor;
  };
  return (
    <Layer name="issue" listening={clickable || dropArmed}>
      {onFrame.map((issue) => {
        if (highlightId !== issue.id) return null;
        const x = issue.anchor_position.x * size.w;
        const y = issue.anchor_position.y * size.h;
        return (
          <Circle
            key={`issue-ring-${issue.id}`}
            name={`video-issue-pin-ring-${issue.id}`}
            x={x}
            y={y}
            radius={radius + highlightRing}
            stroke={highlightColor}
            strokeWidth={pinStroke}
            listening={false}
          />
        );
      })}
      {onFrame.map((issue) => {
        const x = issue.anchor_position.x * size.w;
        const y = issue.anchor_position.y * size.h;
        const fill = pinColors[issuePinColorVar(issue.status, issue.severity)];
        const isHighlight = highlightId === issue.id;
        return (
          <Circle
            key={`issue-${issue.id}`}
            name={`video-issue-pin-${issue.id}`}
            x={x}
            y={y}
            radius={radius}
            fill={fill}
            stroke={ringColor}
            strokeWidth={isHighlight ? selectedStroke : pinStroke}
            listening={clickable}
            onPointerDown={(e) => {
              e.cancelBubble = true;
            }}
            onClick={(e) => {
              e.cancelBubble = true;
              onPinClick?.(issue.id);
            }}
            onMouseEnter={(e) => setCursor(e, "pointer")}
            onMouseLeave={(e) => setCursor(e, "")}
            aria-label={issuePinAriaLabel(issue)}
          />
        );
      })}
      {onFrame.map((issue) => (
        <Text
          key={`issue-label-${issue.id}`}
          x={issue.anchor_position.x * size.w - radius}
          y={issue.anchor_position.y * size.h - radius}
          width={radius * 2}
          height={radius * 2}
          text={issuePinSymbol(issue.status, issue.severity)}
          fontSize={symbolSize}
          fontStyle="bold"
          fill={ringColor}
          align="center"
          verticalAlign="middle"
          listening={false}
        />
      ))}
      {dropArmed && (
        <Rect
          name="video-issue-drop-catcher"
          x={0}
          y={0}
          width={size.w}
          height={size.h}
          fill="rgba(0,0,0,0)"
          listening
          onPointerDown={(event) => {
            event.cancelBubble = true;
          }}
          onPointerUp={(event) => {
            event.cancelBubble = true;
          }}
          onClick={(event) => {
            event.cancelBubble = true;
            if (event.evt.button !== 0 || size.w <= 0 || size.h <= 0) return;
            const stage = event.target.getStage();
            const position = stage?.getPointerPosition();
            if (!stage || !position) return;
            const point = stage.getAbsoluteTransform().copy().invert().point(position);
            const x = point.x / size.w;
            const y = point.y / size.h;
            if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > 1 || y < 0 || y > 1)
              return;
            onDrop?.(x, y, frameIndex);
          }}
        />
      )}
    </Layer>
  );
}
