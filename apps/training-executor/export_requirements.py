"""Export the Python layer; CUDA, Torch and Torchvision come from the pinned base."""

from pathlib import Path
import tomllib

root = Path(__file__).parent
lock = tomllib.loads((root / "uv.lock").read_text())
lines = ["# Generated from uv.lock; binary Torch/CUDA packages use the Docker base."]
for package in lock["package"]:
    name = package["name"]
    if name in {
        "aap-training-executor",
        "aap-training-contract",
        "torch",
        "torchvision",
        "triton",
        "cuda-toolkit",
        "cuda-bindings",
        "cuda-pathfinder",
    } or (name.startswith("nvidia-") and name != "nvidia-ml-py"):
        continue
    hashes = sorted(
        {
            artifact["hash"]
            for artifact in [package.get("sdist", {}), *package.get("wheels", [])]
            if "hash" in artifact
        }
    )
    if not hashes:
        raise ValueError(f"No immutable distribution hashes for {name}")
    markers = package.get("resolution-markers", [])
    marker = " ; " + " or ".join(f"({value})" for value in markers) if markers else ""
    lines.append(
        f"{name}=={package['version']}{marker} "
        + " ".join(f"--hash={value}" for value in hashes)
    )
(root / "requirements.lock").write_text("\n".join(lines) + "\n")
