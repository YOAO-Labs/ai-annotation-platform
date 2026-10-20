"""Independent runtime identity for original-pixel mask evaluation receipts."""

import hashlib
from pathlib import Path

from managed_models import digest
from managed_segmentation import verify_runtime
from sample_evaluations import FIXED_OPTIONS, runtime_identity

MAX_RESPONSE_BYTES = 17 * 1024 * 1024
OPTIONS = {**FIXED_OPTIONS, "retina_masks": True}


def qualification_profile(device, storage_identity):
    verify_runtime()
    native = runtime_identity()
    sources = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in (
            "segmentation_evaluations.py",
            "managed_segmentation.py",
            "managed_models.py",
        )
    }
    profile = {
        **native,
        "implementation": "aap-ultralytics-instance-mask",
        "options": OPTIONS,
        "source_digest": digest(
            {"inference": native["source_digest"], "masks": sources}
        ),
        "effective_device": str(device),
        "storage_identity": str(storage_identity),
    }
    return {**profile, "profile_digest": digest(profile)}
