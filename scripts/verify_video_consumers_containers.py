"""Qualify installed retained-video consumers against disposable local services.

Supply freshly built AAP_VIDEO_CONSUMER_{API,MEDIA_CONTROL,MEDIA_WORKER,CLEANUP}_IMAGE
references carrying the module-data-boundary acceptance labels. This command
never builds images or refreshes the primary development stack.
"""

import os

import verify_module_data_boundary as verification


def main():
    os.environ["AAP_VIDEO_CONSUMER_CONTAINER_ACCEPTANCE"] = "1"
    os.environ["AAP_FRAME_CELERY_ACCEPTANCE"] = "1"
    verification.TEST_FILES = (
        "tests/test_video_consumers_containers.py",
        "tests/test_video_input_cleanup_containers.py",
    )
    return verification.main()


if __name__ == "__main__":
    raise SystemExit(main())
