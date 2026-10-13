/** Background listeners yield to Workbench settings and tool menus without consuming events. */
export function isWorkbenchInteractionBlocked(event: Event): boolean {
  if (typeof document === "undefined") return false;
  const selector =
    "[data-workbench-settings], [data-workbench-guide], [data-workbench-hotkeys], [data-workbench-tool-menu], [data-workbench-ai-toolbar], [data-workbench-context-toolbar], [data-workbench-context-primary], [data-workbench-track-context], [data-workbench-tracker-review], [data-workbench-video-tool-confirm], [data-workbench-discussion], [data-workbench-issue-navigation], [data-workbench-issue-create], [data-workbench-polygon-trace], [data-shell-popover], [data-modal]";
  const triggerSelector =
    "[data-workbench-tool-menu-trigger], [data-workbench-context-toolbar-trigger], [data-workbench-guide-trigger], [data-shell-popover-trigger]";
  // Closing may remove the marker before a later window listener sees this same event.
  return (
    event.composedPath().some((target) => {
      if (!(target instanceof Element)) return false;
      if (target.matches(selector)) return true;
      if (target.matches("[data-workbench-context-quick-tools]")) {
        return (
          !(event instanceof KeyboardEvent) ||
          ["Enter", " ", "Escape", "ArrowDown"].includes(event.key)
        );
      }
      if (target.matches("[data-workbench-video-tool-command]")) {
        return (
          !(event instanceof KeyboardEvent) ||
          ["Enter", " ", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(
            event.key,
          )
        );
      }
      // The trigger must protect opening input before the portal exists, while
      // normal canvas shortcuts resume once a closed menu restores focus to it.
      // The shared shell notification popover follows the same rules: its panel
      // element only exists while open, so panel events are fully shielded and
      // Escape handling is owned by ShellPopover itself.
      return (
        target.matches(triggerSelector) &&
        (!(event instanceof KeyboardEvent) || ["Enter", " ", "ArrowDown"].includes(event.key))
      );
    }) ||
    document.querySelector(
      '[data-workbench-settings][data-state="open"], [data-workbench-guide][data-state="open"], [data-workbench-hotkeys][data-state="open"], [data-workbench-tool-menu][data-state="open"], [data-workbench-tool-menu-trigger][data-state="open"], [data-workbench-video-tool-confirm][data-state="open"], [data-workbench-discussion][data-state="open"], [data-workbench-issue-create][data-state="open"], [data-workbench-polygon-slice][data-state="open"], [data-shell-popover], [data-modal][data-state="open"]',
    ) !== null
  );
}
