export function storageAccessDenied(error: unknown): boolean {
  return (
    !!error &&
    typeof error === "object" &&
    "status" in error &&
    [401, 403, 404].includes(Number(error.status))
  );
}
