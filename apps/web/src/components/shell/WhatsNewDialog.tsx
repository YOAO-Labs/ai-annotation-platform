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
  const shownRef = useRef<string | null>(null);
  const prevUserIdRef = useRef<string | null | undefined>(undefined);

  const due = Boolean(
    user && shouldShowReleaseNotes(user.preferences?.ui?.changelog_seen_version, appVersion),
  );

  useEffect(() => {
    const prevUserId = prevUserIdRef.current;
    prevUserIdRef.current = user?.id ?? null;
    if (!user) {
      shownRef.current = null;
      return;
    }
    if (!due) return;
    if (prevUserId !== null) return;
    const key = `${user.id}:${appVersion}`;
    if (shownRef.current === key) return;
    let alive = true;
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
