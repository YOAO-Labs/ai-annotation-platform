"""Qualify both application-role image grant profiles in disposable resources.

The LAN script deliberately replaces legacy grants, so run this dedicated
qualification separately from unrelated module-role cases.
"""

import verify_module_data_boundary as verification

verification.TEST_FILES = ("tests/test_legacy_image_runtime_live.py",)

if __name__ == "__main__":
    raise SystemExit(verification.main())
