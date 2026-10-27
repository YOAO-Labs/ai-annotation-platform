export const instanceMaskBridge = "ultralytics-instance-rle-1";
export type InstanceMaskBridge = typeof instanceMaskBridge;
export const datasetDraftScope = (task: "detect" | "segment", bridge?: InstanceMaskBridge) =>
  bridge ? `${task}:${bridge}` : task;

export const nativeBridgeOf = (bridge: unknown): InstanceMaskBridge | undefined =>
  bridge === instanceMaskBridge ? instanceMaskBridge : undefined;

export function supportsInstanceMaskBridge(catalog?: import("@/api/trainingRuns").TrainingCatalog) {
  return (
    catalog?.items.some(
      (template) =>
        template.framework === "ultralytics" &&
        template.base_models.some((base) => {
          if (base.id !== "yolo11n-seg" || base.task !== "segment") return false;
          const capabilities = base.capabilities;
          if (!capabilities || !("instance_rle" in capabilities)) return false;
          const qualification = capabilities.instance_rle;
          return (
            !!qualification &&
            typeof qualification === "object" &&
            "bridge" in qualification &&
            qualification.bridge === instanceMaskBridge
          );
        }),
    ) === true
  );
}
