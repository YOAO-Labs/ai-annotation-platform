/** Copy the original value, retaining the fallback for browsers without Clipboard API access. */
export async function copyToClipboard(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // A denied Clipboard API write may still permit a user-initiated legacy copy.
  }

  const textarea = document.createElement("textarea");
  const activeElement = document.activeElement;
  textarea.value = value;
  textarea.className = "sr-only";
  textarea.setAttribute("aria-hidden", "true");
  try {
    const modal =
      activeElement instanceof Element
        ? activeElement.closest('[role="dialog"], [role="alertdialog"]')
        : null;
    (modal ?? document.body).appendChild(textarea);
    textarea.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
    if (activeElement instanceof HTMLElement) activeElement.focus({ preventScroll: true });
  }
}
