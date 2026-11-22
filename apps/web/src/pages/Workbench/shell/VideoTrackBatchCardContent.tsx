import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { ActionBar } from "./selectionCard/ActionBar";
import { VideoTrackComposeDialog, type VideoTrackGapMode } from "../stage/VideoTrackComposeDialog";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";
const SUMMARY_CLASS = "flex items-center justify-between gap-2 text-sm text-foreground";
const COUNT_CLASS = "text-brand tabular-nums";
const SELECT_CLASS =
  "appearance-none min-w-24 border border-border rounded-md bg-background text-foreground text-xs py-1 px-1.5";

export interface VideoTrackBatchCardContentProps {
  count: number;
  readOnly: boolean;
  classes?: string[];
  canMerge: boolean;
  canJoin: boolean;
  mergeDisabledReason?: string | null;
  joinDisabledReason?: string | null;
  allLocked: boolean;
  allHidden: boolean;
  onChangeClass: (className: string) => void;
  onBatchTrack?: () => void;
  onToggleHidden: () => void;
  onToggleLock: () => void;
  onMerge: () => void;
  onJoin: (gapMode: VideoTrackGapMode) => void;
  onDelete: () => void;
  onClear: () => void;
}

export function VideoTrackBatchCardContent({
  count,
  readOnly,
  classes,
  canMerge,
  canJoin,
  mergeDisabledReason,
  joinDisabledReason,
  allLocked,
  allHidden,
  onChangeClass,
  onBatchTrack,
  onToggleHidden,
  onToggleLock,
  onMerge,
  onJoin,
  onDelete,
  onClear,
}: VideoTrackBatchCardContentProps) {
  const [joinOpen, setJoinOpen] = useState(false);
  return (
    <div className={BODY_CLASS}>
      <div className={SUMMARY_CLASS}>
        <span>
          已选 <b className={COUNT_CLASS}>{count}</b> 条轨迹
        </span>
        <select
          aria-label="批量改类"
          value=""
          disabled={readOnly || !classes?.length}
          onChange={(e) => {
            if (!e.target.value) return;
            onChangeClass(e.target.value);
            e.currentTarget.value = "";
          }}
          className={SELECT_CLASS}
        >
          <option value="" disabled hidden>
            改类
          </option>
          {(classes ?? []).map((cls) => (
            <option key={cls} value={cls}>
              {cls}
            </option>
          ))}
        </select>
      </div>

      <ActionBar label="批量操作">
        <Button
          variant="ghost"
          size="sm"
          title="批量延展 · 对选中的多条轨迹一次发起，各自回填原轨迹"
          aria-label="批量延展轨迹"
          disabled={readOnly || !onBatchTrack}
          onClick={onBatchTrack}
        >
          <Icon name="bot" size={14} />
          批量延展
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={allHidden ? "批量显示" : "批量隐藏"}
          aria-label={allHidden ? "批量显示" : "批量隐藏"}
          aria-pressed={allHidden}
          onClick={onToggleHidden}
        >
          <Icon name={allHidden ? "eyeOff" : "eye"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={allLocked ? "批量解锁" : "批量锁定"}
          aria-label={allLocked ? "批量解锁" : "批量锁定"}
          aria-pressed={allLocked}
          disabled={readOnly}
          onClick={onToggleLock}
        >
          <Icon name={allLocked ? "lock" : "unlock"} size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={canMerge ? "合并两条同类轨迹" : (mergeDisabledReason ?? "只支持合并两条同类轨迹")}
          aria-label="合并"
          disabled={readOnly || !canMerge}
          onClick={onMerge}
        >
          <Icon name="layers" size={14} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title={
            canJoin
              ? "跳连两条同类且帧号不重叠的轨迹 (补 gap)"
              : (joinDisabledReason ?? "只支持跳连两条同类且帧号不重叠的轨迹")
          }
          aria-label="跳连"
          disabled={readOnly || !canJoin}
          onClick={() => setJoinOpen(true)}
        >
          <Icon name="link" size={14} />
        </Button>
        <Button
          variant="danger"
          size="sm"
          title="批量删除"
          aria-label="批量删除"
          disabled={readOnly}
          onClick={onDelete}
        >
          <Icon name="trash" size={14} />
        </Button>
        <Button variant="ghost" size="sm" title="取消选中" aria-label="取消选中" onClick={onClear}>
          <Icon name="x" size={14} />
        </Button>
      </ActionBar>

      <VideoTrackComposeDialog
        open={joinOpen}
        onCancel={() => setJoinOpen(false)}
        onSubmit={(gapMode) => {
          setJoinOpen(false);
          onJoin(gapMode);
        }}
      />
    </div>
  );
}
