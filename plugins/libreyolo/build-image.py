"""Build a LibreYOLO image using the installed inference plugin SDK."""
import argparse
from pathlib import Path
import re

from aap_inference_plugin.image_build import _locked_names, build_image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--entrypoint", default="aap-libreyolo")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    try:
        names = _locked_names(args.requirements_lock)
        if "aap-libreyolo-plugin" not in names:
            raise ValueError("lock must include aap-libreyolo-plugin")
        if "ultralytics" in names or any(re.match(r"ultralytics[-_.]", p.name, re.I) for p in args.wheelhouse.glob("*.whl")):
            raise ValueError("Ultralytics is not a LibreYOLO plugin dependency")
        build_image(base_image=args.base_image, wheelhouse=args.wheelhouse,
                    requirements_lock=args.requirements_lock, entrypoint=args.entrypoint,
                    tag=args.tag, push=args.push)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
