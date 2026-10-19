import { CopyableError } from "@/components/ui/CopyableError";
import { useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { Checkbox } from "@/components/shadcn/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";

import { useAcceptCapabilityDrift, useCapabilityDriftPreview } from "../useGlobalRegistry";

export function CapabilityDriftReviewDialog({
  open,
  onOpenChange,
  poolId,
  poolName,
  poolEnabled,
  registryId,
  registryName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  poolId: string;
  poolName: string;
  poolEnabled: boolean;
  registryId: string;
  registryName: string;
}): ReactNode {
  const preview = useCapabilityDriftPreview(poolId, registryId, open);
  const accept = useAcceptCapabilityDrift();
  const pushToast = useToastStore((state) => state.push);
  const [enablePool, setEnablePool] = useState(!poolEnabled);

  const submit = () => {
    const candidate = preview.data?.candidate_fingerprint;
    if (!candidate || !preview.data?.can_accept) return;
    accept.mutate(
      {
        poolId,
        registryId,
        payload: {
          expected_candidate_fingerprint: candidate,
          enable_pool: poolEnabled || enablePool,
        },
      },
      {
        onSuccess: () => {
          pushToast({
            msg: preview.data?.has_drift
              ? `已接受「${registryName}」的能力变更并恢复接流`
              : `已重新验证「${registryName}」并恢复接流`,
            kind: "success",
          });
          onOpenChange(false);
        },
        onError: (error) =>
          pushToast({
            msg: "能力变更审核失败",
            sub: (error as Error).message,
            kind: "warning",
          }),
      },
    );
  };

  const data = preview.data;
  const differingFields = data?.differing_fields ?? [];
  const blockingMembers = data?.blocking_members ?? [];
  const canSubmit = Boolean(data?.can_accept && data.candidate_fingerprint && !accept.isPending);

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (accept.isPending) return;
        if (nextOpen) setEnablePool(!poolEnabled);
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>审核「{registryName}」能力变更</DialogTitle>
          <DialogDescription>
            服务池「{poolName}
            」要求成员能力合同一致。确认后会重新探活，并仅在指纹仍与当前审核结果一致时恢复接流。
          </DialogDescription>
        </DialogHeader>

        {preview.isLoading && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Icon name="loader2" size={14} className="animate-spin" />
            正在读取能力差异…
          </div>
        )}

        {preview.isError && (
          <div className="rounded-md border border-status-danger bg-status-danger-soft p-3 text-sm text-status-danger">
            无法读取能力差异：
            <CopyableError message={(preview.error as Error).message} />
          </div>
        )}

        {data && (
          <div className="flex flex-col gap-4">
            <div className="grid gap-2 rounded-md border border-border bg-muted/30 p-3 text-xs">
              <FingerprintRow label="服务池基线" value={data.pool_fingerprint ?? null} />
              <FingerprintRow label="实例当前值" value={data.candidate_fingerprint ?? null} />
            </div>

            <div className="flex flex-col gap-2">
              <span className="text-sm font-medium">路由合同差异</span>
              {differingFields.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {differingFields.map((field) => (
                    <Badge key={field} variant="warning" className="font-mono text-2xs">
                      {field}
                    </Badge>
                  ))}
                </div>
              ) : (
                <span className="text-xs text-muted-foreground">没有检测到能力差异。</span>
              )}
            </div>

            {blockingMembers.length > 0 && (
              <div className="rounded-md border border-status-caution bg-status-caution-soft p-3 text-xs text-status-caution">
                仍有 {blockingMembers.length} 个 active / draining
                成员与候选能力不一致，需先停用或升级这些成员。
              </div>
            )}

            {!data.can_accept && blockingMembers.length === 0 && (
              <div className="rounded-md border border-status-caution bg-status-caution-soft p-3 text-xs text-status-caution">
                当前状态不满足接受条件。请先确认实例健康、成员已因能力漂移被禁用，且能力快照完整。
              </div>
            )}

            {!poolEnabled && (
              <label className="flex items-start gap-2 rounded-md border border-border p-3 text-sm">
                <Checkbox
                  checked={enablePool}
                  onCheckedChange={(checked) => setEnablePool(checked === true)}
                  aria-label="同时启用服务池"
                />
                <span className="flex flex-col gap-0.5">
                  <span className="font-medium">同时启用服务池</span>
                  <span className="text-xs text-muted-foreground">
                    该池在发现漂移时已关闭；取消勾选将只恢复成员，服务池继续保持停用。
                  </span>
                </span>
              </label>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" disabled={accept.isPending} onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button disabled={!canSubmit} onClick={submit}>
            {accept.isPending
              ? "重新验证中…"
              : data?.has_drift
                ? "接受新能力并恢复接流"
                : "重新验证并恢复接流"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FingerprintRow({ label, value }: { label: string; value: string | null }): ReactNode {
  return (
    <div className="grid grid-cols-[88px_minmax(0,1fr)] gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span>{value ? <CopyValueButton value={value} label={`复制${label}哈希`} /> : "不可用"}</span>
    </div>
  );
}
