import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { datasetsApi, type SniffAxisConventionResponse } from "@/api/datasets";
import {
  LIDAR_AXIS_CONVENTIONS,
  type LidarAxisConvention,
} from "@/pages/Workbench/stages/three-d/geometry/axisConvention";
import styles from "./AxisConventionPicker.module.css";

const LABELS: Record<LidarAxisConvention, string> = {
  iso_8855: "ISO 8855 (+X 前 / +Y 左)",
  ros_rep103: "ROS REP-103 (+X 前 / +Y 左)",
  kitti_camera: "KITTI camera (+X 右 / +Y 下 / +Z 前)",
  opencv_camera: "OpenCV camera (+X 右 / +Y 下 / +Z 前)",
  apollo: "Apollo (+X 右 / +Y 前)",
  y_forward: "Y-forward (+X 右 / +Y 前)",
  sustechpoints_demo: "SUSTechPOINTS demo (+X 左 / +Y 后)",
  raw: "Raw (不归一化)",
};

const AXIS_TEXT: Record<LidarAxisConvention, { x: string; y: string; z: string }> = {
  iso_8855: { x: "前", y: "左", z: "上" },
  ros_rep103: { x: "前", y: "左", z: "上" },
  kitti_camera: { x: "右", y: "下", z: "前" },
  opencv_camera: { x: "右", y: "下", z: "前" },
  apollo: { x: "右", y: "前", z: "上" },
  y_forward: { x: "右", y: "前", z: "上" },
  sustechpoints_demo: { x: "左", y: "后", z: "上" },
  raw: { x: "源 X", y: "源 Y", z: "源 Z" },
};

interface Props {
  value: LidarAxisConvention | null | undefined;
  onChange: (value: LidarAxisConvention) => void;
  disabled?: boolean;
  datasetId?: string;
}

function AxisMiniDiagram({ value }: { value: LidarAxisConvention }) {
  const axis = AXIS_TEXT[value];
  return (
    <div className={styles.axisDiagram} aria-hidden>
      <span className={styles.axisToken}>+X {axis.x}</span>
      <span className={styles.axisToken}>+Y {axis.y}</span>
      <span className={styles.axisToken}>+Z {axis.z}</span>
    </div>
  );
}

function formatScore(score: number | null | undefined) {
  if (typeof score !== "number") return "";
  return `${Math.round(score * 100)}%`;
}

function formatAgreement(sniff: SniffAxisConventionResponse) {
  if (!sniff.per_camera?.length || typeof sniff.agreement !== "number") return null;
  if (sniff.agreement >= 1) return null;
  const matched = Math.round(sniff.agreement * sniff.per_camera.length);
  return `${matched}/${sniff.per_camera.length} 个相机一致，侧/后向相机可能误导，已优先正前相机`;
}

export function AxisConventionPicker({ value, onChange, disabled = false, datasetId }: Props) {
  const current = value ?? "iso_8855";
  const [sniffing, setSniffing] = useState(false);
  const [sniff, setSniff] = useState<SniffAxisConventionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSniff = async () => {
    if (!datasetId || sniffing || disabled) return;
    setSniffing(true);
    setError(null);
    try {
      const result = await datasetsApi.sniffAxisConvention(datasetId);
      setSniff(result);
      if (result.best) onChange(result.best);
      else setError("未找到可用于检测的相机标定");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSniffing(false);
    }
  };

  return (
    <div className={styles.root}>
      <div className={styles.row}>
        <select
          value={current}
          disabled={disabled}
          className={styles.select}
          onChange={(e) => onChange(e.target.value as LidarAxisConvention)}
        >
          {LIDAR_AXIS_CONVENTIONS.map((c) => (
            <option key={c} value={c}>
              {LABELS[c]}
            </option>
          ))}
        </select>
        {datasetId && (
          <Button size="sm" onClick={handleSniff} disabled={disabled || sniffing}>
            <Icon name={sniffing ? "loader2" : "search"} size={12} />
            {sniffing ? "检测中" : "自动检测"}
          </Button>
        )}
      </div>
      <AxisMiniDiagram value={current} />
      {sniff?.best && (
        <div className={styles.sniffResult}>
          建议 {LABELS[sniff.best]} · 匹配度 {formatScore(sniff.score)}
          {typeof sniff.score === "number" && sniff.score < 0.85 ? " · 建议人工核对" : ""}
        </div>
      )}
      {sniff && formatAgreement(sniff) && (
        <div className={styles.agreement}>{formatAgreement(sniff)}</div>
      )}
      {sniff?.candidates && sniff.candidates.length > 1 && (
        <div className={styles.candidates}>
          {sniff.candidates.slice(0, 3).map((c) => (
            <span key={c.convention}>
              {LABELS[c.convention]} {formatScore(c.score)}
            </span>
          ))}
        </div>
      )}
      {error && (
        <div className={styles.error}>
          <CopyableError message={error} />
        </div>
      )}
    </div>
  );
}
