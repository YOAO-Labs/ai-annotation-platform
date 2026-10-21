from aap_inference_contract.capabilities import CapabilityLimits

from app.config import settings


def capability_limits_from_settings() -> CapabilityLimits:
    return CapabilityLimits(
        video_tracker_window_size_frames=settings.video_tracker_window_size_frames,
        video_tracker_sam3_window_size_frames=settings.video_tracker_sam3_window_size_frames,
    )
