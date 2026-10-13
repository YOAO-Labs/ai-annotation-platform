/**
 * 版本更新提醒:登录瞬间(应用生命周期内账号从无到有)且账号未确认过当前构建
 * 版本时,弹一次「本次更新」窗口,展示 CHANGELOG 中当前版本的要点。刷新恢复
 * 会话不自动弹出,未确认版本留到下次真正登录再提醒。
 *
 * 顶栏版本号(主界面 TopBar 与全屏工作台 Topbar)可随时手动打开本窗口查看更新
 * 内容。已确认版本存在服务端偏好(ui.changelog_seen_version),跨设备去重;点
 * 「知道了」仅在当前版本更高时单调写回并关闭,已确认更高版本的旧前端不会把
 * 跨设备标记改小。直接关闭(X / Esc / 遮罩)不写库,本次登录不再弹,下次登录
 * 时若仍未确认会再弹;账号退出会清空本次已弹登记,同一账号不刷新重登仍会提醒。
 * 挂载在 App 级,登录后无论落在主界面还是全屏工作台都能弹出(Radix 传送门)。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { create } from "zustand";
import { PartyPopperIcon } from "lucide-react";
import { Button } from "@/components/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";
import { authApi } from "@/api/auth";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import {
  appVersion,
  compareSemver,
  loadCurrentReleaseNotes,
  shouldShowReleaseNotes,
  type ReleaseNotes,
} from "@/utils/releaseNotes";

interface WhatsNewUiState {
  /** 顶栏版本号手动打开;与登录自动提醒独立,随时可看、不看已读状态。 */
  manualOpen: boolean;
  openManually: () => void;
  closeUi: () => void;
}

export const useWhatsNewStore = create<WhatsNewUiState>((set) => ({
  manualOpen: false,
  openManually: () => set({ manualOpen: true }),
  closeUi: () => set({ manualOpen: false }),
}));

export function WhatsNewDialog() {
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const manualOpen = useWhatsNewStore((s) => s.manualOpen);
  const closeUi = useWhatsNewStore((s) => s.closeUi);
  const [autoOpen, setAutoOpen] = useState(false);
  const [notes, setNotes] = useState<ReleaseNotes | null>(null);
  // 已自动弹过的「用户+版本」;只在真正弹出时登记:StrictMode 的
  // setup-cleanup-setup 双调用、或 user 对象被替换重取导致 effect 重跑时,
  // 重试本次提醒而不是把它吞掉。
  const shownRef = useRef<string | null>(null);
  // 上一次观察到的账号 id:应用生命周期内从无到有 = 登录(含跨标签页采用);
  // 挂载时账号已存在 = 刷新恢复会话,不自动弹。
  const prevUserIdRef = useRef<string | null | undefined>(undefined);

  const due = Boolean(
    user && shouldShowReleaseNotes(user.preferences?.ui?.changelog_seen_version, appVersion),
  );

  useEffect(() => {
    const prevUserId = prevUserIdRef.current;
    prevUserIdRef.current = user?.id ?? null;
    if (!user) {
      // 账号退出(会话结束):清掉已弹登记,同一账号再次登录时仍会提醒——
      // 否则「关闭弹窗 → 登出 → 不刷新直接重登」会被同一 user+版本键吞掉。
      shownRef.current = null;
      return;
    }
    if (!due) return;
    // 仅登录瞬间提醒;prevUserId 为 undefined(挂载时已带会话)或已有账号
    // (user 对象被重取替换)都跳过,未确认版本留到下次真正登录。
    if (prevUserId !== null) return;
    const key = `${user.id}:${appVersion}`;
    if (shownRef.current === key) return;
    let alive = true;
    // 内容按需加载(独立 chunk);失败也弹窗,回落为简短提示。
    void loadCurrentReleaseNotes()
      .then((loaded) => {
        if (!alive) return;
        shownRef.current = key;
        setNotes(loaded);
        setAutoOpen(true);
      })
      .catch(() => {
        if (!alive) return;
        shownRef.current = key;
        setNotes(null);
        setAutoOpen(true);
      });
    return () => {
      alive = false;
    };
  }, [due, user]);

  // 手动打开(顶栏版本号):随时可看,不要求未确认版本,也不自动写已读。
  useEffect(() => {
    if (!manualOpen) return;
    void loadCurrentReleaseNotes()
      .then((loaded) => setNotes((prev) => prev ?? loaded))
      .catch(() => {});
  }, [manualOpen]);

  const close = useCallback(() => {
    setAutoOpen(false);
    closeUi();
  }, [closeUi]);

  const acknowledge = useCallback(async () => {
    const ownerId = user?.id;
    if (!ownerId || !isCurrentAuthOwner(ownerId)) return;
    const currentUser = useAuthStore.getState().user;
    if (!currentUser || currentUser.id !== ownerId) return;
    // 单调保护:账号已确认过更新(或更高)的版本时不再回写。回滚或缓存了旧前端的
    // 设备手动点「知道了」不能把跨设备已读版本改小,否则较新版本会重新提醒。
    if (compareSemver(appVersion, currentUser.preferences?.ui?.changelog_seen_version ?? "") <= 0) {
      return;
    }
    try {
      const preferences = await authApi.updatePreferences({
        ui: { changelog_seen_version: appVersion },
      });
      if (!isCurrentAuthOwner(ownerId)) return;
      const latest = useAuthStore.getState().user;
      if (!latest || latest.id !== ownerId) return;
      // 只并入已确认版本,保留其它并发 writer(主题/面板显隐)更新的 ui 状态:
      // 延迟到达的响应若整块替换 ui,会把用户新选择回退到旧快照。
      setUser({
        ...latest,
        preferences: {
          ...latest.preferences,
          ui: {
            ...latest.preferences?.ui,
            changelog_seen_version: preferences.ui?.changelog_seen_version ?? appVersion,
          },
        },
      });
    } catch {
      // 写入失败保持未读:下次登录再提醒一次,不打断当前操作。
    }
  }, [setUser, user?.id]);

  return (
    <Dialog open={autoOpen || manualOpen} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <PartyPopperIcon className="size-5 text-primary" aria-hidden="true" />v
            {notes?.version ?? appVersion}
          </DialogTitle>
          <DialogDescription>
            {notes?.date ? `发布于 ${notes.date} · ` : ""}
            每个版本只在登录时提醒一次,点击「知道了」后不再提醒;顶栏版本号可随时查看。
          </DialogDescription>
        </DialogHeader>
        {notes && notes.groups.length > 0 ? (
          <div className="max-h-[55vh] space-y-5 overflow-y-auto pr-1">
            {notes.groups.map((group) => (
              <section key={group.key}>
                <h3 className="text-xs font-semibold tracking-wider text-muted-foreground uppercase">
                  {group.label}
                </h3>
                <ul className="mt-2 list-disc space-y-2 pl-5 marker:text-muted-foreground/70">
                  {group.items.map((item, index) => (
                    <li key={index} className="text-sm leading-relaxed">
                      {item}
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            本次更新内容整理中,可稍后在文档站查看完整更新日志。
          </p>
        )}
        <DialogFooter>
          <Button
            onClick={() => {
              close();
              void acknowledge();
            }}
          >
            知道了
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
