"""Build a wheel with the canonical controlled asset manifest embedded.

Run from a source checkout; generated wheel is independently installable with
its versioned dependency wheels. The staged manifest is not a second source.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
plugin = Path(__file__).resolve().parent
manifest = root / "apps/yolo-backend/scripts/controlled-model-assets.json"
out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else plugin / "dist"
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
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(staging)],
        check=True,
    )
