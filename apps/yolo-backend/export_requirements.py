"""Export hashed Python wheels; Torch/CUDA and the pinned CLIP source are separate."""

from pathlib import Path
import tomllib

root = Path(__file__).parent
lock = tomllib.loads((root / "uv.lock").read_text())
lines = [
    "# Generated from uv.lock; binary packages come from the immutable Docker base."
]
excluded = {
    "yolo-backend",
    "aap-backend-runtime",
    "aap-protocol-v2",
    "aap-training-contract",
    "aap-task-contract",
    "clip",
    "torch",
    "torchvision",
    "triton",
    "cuda-toolkit",
    "cuda-bindings",
    "cuda-pathfinder",
}
for package in lock["package"]:
    name = package["name"]
    if name in excluded or (name.startswith("nvidia-") and name != "nvidia-ml-py"):
        continue
    hashes = sorted(
        {
            a["hash"]
            for a in [package.get("sdist", {}), *package.get("wheels", [])]
            if "hash" in a
        }
    )
    if not hashes:
        raise ValueError(f"No immutable distributions for {name}")
    markers = package.get("resolution-markers", [])
    marker = " ; " + " or ".join(f"({value})" for value in markers) if markers else ""
    lines.append(
        f"{name}=={package['version']}{marker} "
        + " ".join(f"--hash={value}" for value in hashes)
    )
(root / "requirements.lock").write_text("\n".join(lines) + "\n")
