"""Qualify installed retained-video consumers against disposable local services.

Supply freshly built AAP_VIDEO_CONSUMER_{API,MEDIA_CONTROL,MEDIA_WORKER,CLEANUP}_IMAGE
references carrying the module-data-boundary acceptance labels. This command
never builds images or refreshes the primary development stack.

The default smoke profile remains small. ``--profile capacity`` runs the fixed
three-task, 120-frame-per-task, 1280x720 load and graceful in-flight Celery stop.
``--profile media-handoff`` selects only the paused Media lease takeover case
and requires only AAP_VIDEO_CONSUMER_MEDIA_WORKER_IMAGE.
"""

import argparse
import os

import verify_module_data_boundary as verification


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--profile", choices=("smoke", "capacity", "media-handoff"), default="smoke"
    )
    arguments, verifier_arguments = parser.parse_known_args(argv)
    if "-h" in verifier_arguments or "--help" in verifier_arguments:
        print(
            "usage: verify_video_consumers_containers.py "
            "[--profile smoke|capacity|media-handoff] --output PATH [--junit-xml PATH]"
        )
        return 0
    os.environ["AAP_VIDEO_CONSUMER_PROFILE"] = (
        "smoke" if arguments.profile == "media-handoff" else arguments.profile
    )
    os.environ["AAP_VIDEO_CONSUMER_CONTAINER_ACCEPTANCE"] = "1"
    os.environ["AAP_FRAME_CELERY_ACCEPTANCE"] = "1"
    if arguments.profile == "media-handoff":
        verification.TEST_FILES = ("tests/test_video_input_handoff_containers.py",)
    else:
        verification.TEST_FILES = (
            "tests/test_video_consumers_containers.py",
            "tests/test_video_input_cleanup_containers.py",
        )
    return verification.main(verifier_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
