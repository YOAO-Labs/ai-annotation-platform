/** Retained data is useful after network failures, but never after access is revoked. */
export function storageAccessDenied(error: unknown): boolean {
  return (
    !!error &&
    typeof error === "object" &&
    "status" in error &&
    [401, 403, 404].includes(Number(error.status))
  );
}
