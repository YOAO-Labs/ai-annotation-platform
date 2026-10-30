"""Build the LibreYOLO wheel with an explicitly supplied canonical asset manifest."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("out", type=Path)
parser.add_argument("--manifest", type=Path, required=True)
args = parser.parse_args()
plugin = Path(__file__).resolve().parent
manifest = args.manifest.resolve(strict=True)
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="libreyolo-wheel-") as temporary:
    staging = Path(temporary) / "plugin"
    shutil.copytree(
        plugin,
        staging,
        ignore=shutil.ignore_patterns(
            "dist", ".venv", "uv.lock", "__pycache__", "*.egg-info"
        ),
    )
    shutil.copyfile(
        manifest, staging / "aap_libreyolo_plugin/controlled-model-assets.json"
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out),
            str(staging),
        ],
        check=True,
    )
