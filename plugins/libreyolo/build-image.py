"""Build and push an image from an approved digest and a complete locked wheelhouse."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--base-image", required=True)
parser.add_argument("--wheelhouse", type=Path, required=True)
parser.add_argument("--tag", required=True)
args = parser.parse_args()
if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", args.base_image):
    parser.error("base image must be pinned by sha256 digest")
if not args.wheelhouse.is_dir():
    parser.error("an existing locked wheelhouse is required")
wheels = list(args.wheelhouse.glob("*.whl"))
if not wheels or not any(p.name.startswith("aap_libreyolo_plugin-") for p in wheels):
    parser.error("wheelhouse must contain the plugin and its complete dependency closure")
if any(p.name.lower().startswith("ultralytics") for p in wheels):
    parser.error("Ultralytics is not a LibreYOLO plugin dependency")
with tempfile.TemporaryDirectory(prefix="libreyolo-image-") as temporary:
    context = Path(temporary)
    shutil.copyfile(Path(__file__).with_name("Dockerfile"), context / "Dockerfile")
    shutil.copytree(args.wheelhouse, context / "wheelhouse")
    subprocess.run(["docker", "build", "--build-arg", f"BASE_IMAGE={args.base_image}", "--tag", args.tag, str(context)], check=True)
    subprocess.run(["docker", "push", args.tag], check=True)
print("Image pushed. Generate installation manifest with release-manifest.py after inspecting the pushed digest.")
