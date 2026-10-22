export function parseReference(path: string): { itemId: string; sha256: string } | null {
  const match = /^__aap_reference__\/([0-9a-f-]{36})\/([0-9a-f]{64})$/.exec(path);
  return match ? { itemId: match[1], sha256: match[2] } : null;
}
