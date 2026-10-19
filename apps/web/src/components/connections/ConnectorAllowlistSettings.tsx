import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { Alert, AlertDescription, AlertTitle } from "@/components/shadcn/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/shadcn/ui/field";
import { Input } from "@/components/shadcn/ui/input";
import {
  useConnectorAllowlist,
  useResetConnectorAllowlist,
  useUpdateConnectorAllowlist,
} from "@/hooks/useStorageConnections";

type ConfirmMode = "empty" | "reset" | null;

function sameEntries(left: string[], right: string[]) {
  return left.length === right.length && left.every((entry, index) => entry === right[index]);
}

function validateEntry(raw: string): string | null {
  const entry = raw.trim();
  if (!entry) return "请输入主机、域名或 CIDR";
  if (entry.length > 253) return "单条不能超过 253 个字符";
  if (entry.includes("://")) return "请只输入 host，不要包含 URL scheme";
  if (entry.includes("*")) return "不支持 * 通配符，后缀域名请写成 .example.com";
  if (entry.includes("\\")) return "条目不能包含路径";
  if (/^[^:]+:\d+$/.test(entry)) return "条目不能包含端口";
  if (entry === ".") return "后缀域名不能为空";
  if (entry.includes("/") && !/^[0-9a-fA-F:.]+\/\d{1,3}$/.test(entry)) {
    return "路径或 CIDR 格式无效";
  }
  return null;
}

function normalizeDraftEntry(raw: string) {
  const entry = raw.trim();
  if (entry.includes(":") || entry.includes("/")) return entry;
  const suffix = entry.startsWith(".");
  const domain = (suffix ? entry.slice(1) : entry).replace(/\.$/, "").toLowerCase();
  return suffix ? `.${domain}` : domain;
}

export function ConnectorAllowlistSettings() {
  const query = useConnectorAllowlist();
  const updateMutation = useUpdateConnectorAllowlist();
  const resetMutation = useResetConnectorAllowlist();
  const pushToast = useToastStore((state) => state.push);
  const [entries, setEntries] = useState<string[]>([]);
  const [initialized, setInitialized] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null);

  useEffect(() => {
    if (!query.data || initialized) return;
    setEntries(query.data.entries);
    setInitialized(true);
  }, [initialized, query.data]);

  const addEntry = (event: FormEvent) => {
    event.preventDefault();
    const error = validateEntry(draft);
    if (error) {
      setDraftError(error);
      return;
    }
    const entry = normalizeDraftEntry(draft);
    if (entries.includes(entry)) {
      setDraftError("该条目已存在");
      return;
    }
    if (entries.length >= 256) {
      setDraftError("白名单最多 256 条");
      return;
    }
    setEntries((current) => [...current, entry]);
    setDraft("");
    setDraftError(null);
  };

  const save = async () => {
    try {
      const result = await updateMutation.mutateAsync(entries);
      setEntries(result.entries);
      pushToast({ msg: "连接器主机白名单已保存", kind: "success" });
    } catch (error) {
      pushToast({ msg: "白名单保存失败", sub: (error as Error).message, kind: "warning" });
    }
  };

  const reset = async () => {
    try {
      const result = await resetMutation.mutateAsync();
      setEntries(result.entries);
      pushToast({ msg: "已恢复部署默认白名单", kind: "success" });
    } catch (error) {
      pushToast({ msg: "恢复部署默认失败", sub: (error as Error).message, kind: "warning" });
    }
  };

  const confirm = () => {
    const mode = confirmMode;
    setConfirmMode(null);
    if (mode === "empty") void save();
    if (mode === "reset") void reset();
  };

  if (query.isError || (!query.isLoading && !query.data)) {
    return (
      <Card>
        <div className="border-b border-border px-4 py-3">
          <h3 className="m-0 text-sm font-semibold">连接器主机白名单</h3>
        </div>
        <div className="flex flex-col gap-3 p-4">
          <Alert variant="destructive">
            <Icon name="warning" />
            <AlertTitle>无法读取白名单</AlertTitle>
            <AlertDescription>
              <CopyableError message={(query.error as Error | null)?.message ?? "请刷新后重试"} />
            </AlertDescription>
          </Alert>
          <Button className="self-start" onClick={() => void query.refetch()}>
            重试
          </Button>
        </div>
      </Card>
    );
  }

  if (query.isLoading || !initialized) {
    return (
      <Card>
        <div className="border-b border-border px-4 py-3">
          <h3 className="m-0 text-sm font-semibold">连接器主机白名单</h3>
        </div>
        <div className="p-4 text-sm text-muted-foreground">加载中...</div>
      </Card>
    );
  }

  if (!query.data) {
    return null;
  }

  const dirty = !sameEntries(entries, query.data.entries);
  const pending = updateMutation.isPending || resetMutation.isPending;
  const mutationError = updateMutation.error ?? resetMutation.error;

  return (
    <>
      <Card>
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex flex-col gap-1">
            <h3 className="m-0 text-sm font-semibold">连接器主机白名单</h3>
            <p className="m-0 text-xs leading-relaxed text-muted-foreground">
              控制 S3、OSS 与 SFTP 连接器可访问的目标主机
            </p>
          </div>
          <Badge variant={query.data.source === "database" ? "warning" : "outline"}>
            {query.data.source === "database" ? "数据库覆盖" : "部署默认"}
          </Badge>
        </div>

        <div className="flex flex-col gap-4 p-4">
          <Alert variant="warning">
            <Icon name="warning" />
            <AlertTitle>空名单会拒绝全部连接器出网</AlertTitle>
            <AlertDescription>
              Loopback、link-local、multicast、未指定和保留地址始终被拒绝。CIDR
              或域名范围越宽，授权面越大。
            </AlertDescription>
          </Alert>

          <FieldGroup className="gap-3">
            <Field data-invalid={draftError ? true : undefined}>
              <FieldLabel htmlFor="connector-allowlist-entry">添加条目</FieldLabel>
              <form className="flex items-start gap-2" onSubmit={addEntry}>
                <Input
                  id="connector-allowlist-entry"
                  value={draft}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    if (draftError) setDraftError(null);
                  }}
                  aria-invalid={draftError ? true : undefined}
                  placeholder="10.0.3.0/24 或 .aliyuncs.com"
                  maxLength={253}
                  disabled={pending}
                />
                <Button type="submit" size="sm" disabled={pending || entries.length >= 256}>
                  <Icon name="plus" />
                  添加
                </Button>
              </form>
              <FieldDescription>
                支持单 IP、CIDR、精确域名和前导点后缀域名，不含 scheme、端口或路径。
              </FieldDescription>
              <FieldError>{draftError}</FieldError>
            </Field>
          </FieldGroup>

          <div className="flex flex-col gap-2" aria-label="当前白名单条目">
            {entries.length === 0 ? (
              <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">
                当前为空，保存后所有连接器创建、更新、测试与导入都会被拒绝。
              </div>
            ) : (
              entries.map((entry) => (
                <div
                  key={entry}
                  className="flex min-h-10 items-center justify-between gap-3 rounded-md border border-border bg-muted/40 px-3 py-1.5"
                >
                  <code className="min-w-0 break-all text-sm text-foreground">{entry}</code>
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    aria-label={`移除 ${entry}`}
                    onClick={() =>
                      setEntries((current) => current.filter((item) => item !== entry))
                    }
                    disabled={pending}
                  >
                    移除
                  </Button>
                </div>
              ))
            )}
          </div>

          {mutationError && (
            <Alert variant="destructive">
              <Icon name="warning" />
              <AlertTitle>操作未完成</AlertTitle>
              <AlertDescription>
                <CopyableError message={(mutationError as Error).message} />
              </AlertDescription>
            </Alert>
          )}

          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              onClick={() => setConfirmMode("reset")}
              disabled={query.data.source !== "database" || pending}
            >
              恢复部署默认
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => (entries.length === 0 ? setConfirmMode("empty") : void save())}
              disabled={!dirty || pending}
            >
              {updateMutation.isPending ? "保存中..." : "保存覆盖"}
            </Button>
          </div>
        </div>
      </Card>

      <AlertDialog
        open={confirmMode !== null}
        onOpenChange={(open) => !open && setConfirmMode(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmMode === "empty" ? "保存空白名单？" : "恢复部署默认白名单？"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmMode === "empty"
                ? "保存后，连接器创建、更新、测试和导入都会被拒绝，直到重新添加允许条目。"
                : "数据库覆盖会被删除，平台立即回退到 CONNECTOR_HOST_ALLOWLIST 的部署配置。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={confirm}>
              {confirmMode === "empty" ? "确认保存空名单" : "确认恢复"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
