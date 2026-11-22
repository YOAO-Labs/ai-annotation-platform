interface ContextToolbarEligibility {
  maskActive: boolean;
  interactiveActive: boolean;
  seedCollecting: boolean;
  editingPending: boolean;
  trackerReviewAvailable: boolean;
  secondaryAvailable: boolean;
  capabilityRecovery: boolean;
}

export function resolveContextToolbar(eligibility: ContextToolbarEligibility) {
  if (eligibility.maskActive) return "mask";
  if (eligibility.seedCollecting) return null;
  if (eligibility.interactiveActive) return "interactive";
  if (eligibility.trackerReviewAvailable && !eligibility.editingPending) return "tracker";
  if (eligibility.secondaryAvailable) return "secondary";
  if (eligibility.capabilityRecovery) return "recovery";
  return null;
}
