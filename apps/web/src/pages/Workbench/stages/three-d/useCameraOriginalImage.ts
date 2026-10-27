import { useEffect, useState } from "react";
import { originalOwnerKey } from "@/api/originals";
import { acquireCameraImageAddress } from "./pointCloudAssetCache";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";

export function useCameraOriginalImage(source: string | null | undefined) {
  const ownerKey = useOriginalAssetOwner();
  const key = JSON.stringify([ownerKey, source]);
  const [result, setResult] = useState<{ key: string; url?: string; failed?: boolean }>();
  useEffect(() => {
    if (!source) return;
    const controller = new AbortController();
    let release: (() => void) | undefined;
    void acquireCameraImageAddress(source, controller.signal).then(
      (address) => {
        if (controller.signal.aborted || ownerKey !== originalOwnerKey()) {
          address.release();
          return;
        }
        release = address.release;
        setResult({ key, url: address.url });
      },
      () => {
        if (!controller.signal.aborted && ownerKey === originalOwnerKey())
          setResult({ key, failed: true });
      },
    );
    return () => {
      controller.abort();
      release?.();
    };
  }, [key, ownerKey, source]);
  return result?.key === key ? result : { key };
}
