import {
  describeOriginal,
  isOriginalOwnerCurrent,
  promoteOriginalRead,
  readOriginal,
  type OriginalIdentity,
  type OriginalOwner,
  type OriginalResource,
} from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";

const PREFETCH_BYTES = 64 * 1024 * 1024;
const READY_ENTRIES = 2;
type Download = { identity: OriginalIdentity; blob: Blob };
interface Entry {
  owner: OriginalOwner;
  controller: AbortController;
  consumers: Set<symbol>;
  pending: Promise<Download>;
  ready: Download | null;
}
const entries = new Map<string, Entry>();

function aborted() {
  return new DOMException("Image owner retired", "AbortError");
}
function retire(key: string, entry: Entry) {
  if (entries.get(key) === entry) entries.delete(key);
  entry.controller.abort();
  entry.ready = null;
}
const unsubscribe = useAuthStore.subscribe(() => {
  for (const [key, entry] of entries) if (!isOriginalOwnerCurrent(entry.owner)) retire(key, entry);
});
import.meta.hot?.dispose(() => {
  unsubscribe();
  for (const [key, entry] of entries) retire(key, entry);
});

function trim() {
  let count = 0,
    bytes = 0;
  for (const [key, entry] of [...entries].reverse()) {
    if (!entry.ready) continue;
    count += 1;
    bytes += entry.ready.blob.size;
    if (count > READY_ENTRIES || bytes > PREFETCH_BYTES) entries.delete(key);
  }
}

/** Re-describe every consumer; cached bytes never grant authority by themselves. */
export async function readOriginalImage(
  resource: OriginalResource,
  options: { owner: OriginalOwner; signal: AbortSignal; priority?: "active" | "prefetch" },
): Promise<Download | null> {
  const { owner, signal, priority = "active" } = options;
  const current = () => !signal.aborted && isOriginalOwnerCurrent(owner);
  if (!current()) throw aborted();
  const identity = await describeOriginal(resource, { owner, signal });
  if (!current()) throw aborted();
  if (priority === "prefetch" && identity.size_bytes > PREFETCH_BYTES) return null;
  const key = JSON.stringify([owner.userId, owner.sessionId, resource, identity.download_path]);
  let entry = entries.get(key);
  if (entry?.controller.signal.aborted) {
    retire(key, entry);
    entry = undefined;
  }
  if (entry?.ready) {
    entries.delete(key);
    entries.set(key, entry);
    return { identity, blob: entry.ready.blob };
  }
  if (!entry) {
    const controller = new AbortController();
    entry = { owner, controller, consumers: new Set(), ready: null, pending: null! };
    const owned = entry;
    owned.pending = readOriginal(resource, { owner, signal: controller.signal, priority, identity })
      .then((download) => {
        if (controller.signal.aborted || !isOriginalOwnerCurrent(owner)) throw aborted();
        if (download.blob.size <= PREFETCH_BYTES && entries.get(key) === owned) {
          owned.ready = download;
          trim();
        } else if (entries.get(key) === owned) entries.delete(key);
        return download;
      })
      .catch((error) => {
        retire(key, owned);
        throw error;
      });
    entries.set(key, owned);
  }
  const owned = entry;
  if (priority === "active") promoteOriginalRead(owned.controller.signal);
  const consumer = Symbol();
  owned.consumers.add(consumer);
  return new Promise((resolve, reject) => {
    let settled = false;
    const release = () => {
      signal.removeEventListener("abort", abort);
      owned.consumers.delete(consumer);
      if (!owned.ready && owned.consumers.size === 0) retire(key, owned);
    };
    const abort = () => {
      if (settled) return;
      settled = true;
      release();
      reject(aborted());
    };
    signal.addEventListener("abort", abort, { once: true });
    void owned.pending.then(
      (download) => {
        if (settled) return;
        settled = true;
        release();
        if (current()) resolve({ identity, blob: download.blob });
        else reject(aborted());
      },
      (error) => {
        if (settled) return;
        settled = true;
        release();
        reject(current() ? error : aborted());
      },
    );
    if (!current()) abort();
  });
}
