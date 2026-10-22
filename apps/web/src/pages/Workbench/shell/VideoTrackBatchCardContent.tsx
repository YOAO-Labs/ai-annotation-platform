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
  /** 任务级锁定(review/completed)→ 改类 / 锁定 / 合并 / 跳连 / 删除 只读。 */
  readOnly: boolean;
  classes?: string[];
  /** 恰好选中 2 条同类轨迹。 */
  canMerge: boolean;
  /** 恰好选中 2 条同类且可见帧区间不重叠的轨迹。 */
  canJoin: boolean;
  mergeDisabledReason?: string | null;
  joinDisabledReason?: string | null;
  /** 选中的轨迹是否全部已锁定 / 已隐藏 —— 决定切换按钮的图标与文案。 */
  allLocked: boolean;
  allHidden: boolean;
  onChangeClass: (className: string) => void;
  /** v0.22.2 · M2 · 批量 AI 追踪: 对选中的 ≥2 条轨迹一次发起多源追踪 (单 job 各回填各自源)。
   *  只读 (review/completed) 或未接线时省略 → 按钮灰置。 */
  onBatchTrack?: () => void;
  onToggleHidden: () => void;
  onToggleLock: () => void;
  onMerge: () => void;
  onJoin: (gapMode: VideoTrackGapMode) => void;
  onDelete: () => void;
  onClear: () => void;
}

/**
 * 选中信息卡的「多选轨迹」批量内容。此前多选 ≥2 条轨迹时浮卡只显示最后选中那条的单卡,
 * 批量操作只能去右栏 roster —— 同一交互对象从 1 选到 2 选操作整片换位。此卡把 roster 批量条
 * (改类 / 显隐 / 锁 / 合并 / 跳连 / 删除)搬到浮卡, 与 roster 对等、同一批 shell handler。
 * 跳连沿用 VideoTrackComposeDialog 选 gap 模式。
 *
 * 显隐 / 锁定各占一个**切换**按钮 (图标与文案随 allHidden / allLocked 翻转), 与图片工作台
 * 及视频单帧的批量卡一致 —— 而不是显示/隐藏/锁定/解锁四个并排按钮。
 */
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
