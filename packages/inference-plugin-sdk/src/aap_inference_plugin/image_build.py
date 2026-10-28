"""Build a plugin image from an offline, hash-locked wheelhouse."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

_SCRIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BASE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")
_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._-]+\])?==([^\s;]+)(?:\s*;\s*.+?)?\s+(--hash=sha256:[0-9a-fA-F]{64}(?:\s+--hash=sha256:[0-9a-fA-F]{64})*)\Z")


def _locked_names(lock: Path) -> set[str]:
    """Accept only pinned requirements with SHA-256 hashes, not pip directives or URLs."""
    lines = []
    pending = ""
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
        else:
            lines.append(pending + line)
            pending = ""
    if pending or not lines:
        raise ValueError("requirements lock must contain complete pinned, hashed requirements")
    names = set()
    for line in lines:
        match = _REQUIREMENT.fullmatch(line)
        if not match or any(c in match.group(2) for c in "@/#\\") or "*" in match.group(2):
            raise ValueError("requirements lock must contain only pinned requirements with SHA-256 hashes")
        names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def build_image(*, base_image: str, wheelhouse: Path, requirements_lock: Path,
                entrypoint: str, tag: str, push: bool = False) -> None:
    """Build from a digest-pinned base; pip verifies every locked offline dependency.

    The lock must include the author distribution that owns the selected script.
    Docker and a wheelhouse containing the entire dependency closure are required.
    """
    wheelhouse = Path(wheelhouse)
    requirements_lock = Path(requirements_lock)
    if not _BASE.fullmatch(base_image):
        raise ValueError("base image must be pinned by sha256 digest")
    if not _SCRIPT.fullmatch(entrypoint):
        raise ValueError("entrypoint must be a bare console script name")
    if not tag or tag.startswith("-") or any(c.isspace() for c in tag):
        raise ValueError("invalid image tag")
    if not wheelhouse.is_dir() or not list(wheelhouse.glob("*.whl")):
        raise ValueError("wheelhouse must contain wheels")
    if not requirements_lock.is_file():
        raise ValueError("requirements lock must be a file")
    names = sorted(_locked_names(requirements_lock))
    command = [entrypoint, "serve", "--host", "0.0.0.0", "--port", "8000"]
    # Check metadata rather than merely finding a same-named executable in the base image.
    check = ("import importlib.metadata as m, re; "
             f"names={names!r}; script={entrypoint!r}; "
             "assert any(re.sub(r'[-_.]+', '-', d.metadata['Name']).lower() in names "
             "and any(e.group == 'console_scripts' and e.name == script for e in d.entry_points) "
             "for d in m.distributions()), 'selected console script is not provided by a locked distribution'")
    dockerfile = f'''ARG BASE_IMAGE
FROM ${{BASE_IMAGE}}
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN --network=none --mount=type=bind,source=wheelhouse,target=/wheelhouse --mount=type=bind,source=requirements.lock,target=/requirements.lock python -m pip install --no-cache-dir --ignore-installed --no-index --find-links=/wheelhouse --only-binary=:all: --require-hashes -r /requirements.lock && python -c {json.dumps(check)} && groupadd -g 10001 plugin && useradd -u 10001 -g 10001 -M plugin && mkdir -p /var/lib/aap-libreyolo && chown 10001:10001 /var/lib/aap-libreyolo
USER 10001:10001
EXPOSE 8000
ENTRYPOINT {json.dumps(command)}
CMD []
'''
    with tempfile.TemporaryDirectory(prefix="aap-plugin-image-") as temporary:
        context = Path(temporary)
        (context / "wheelhouse").mkdir()
        for wheel in wheelhouse.glob("*.whl"):
            shutil.copyfile(wheel, context / "wheelhouse" / wheel.name)
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        shutil.copyfile(requirements_lock, context / "requirements.lock")
        subprocess.run(["docker", "build", "--build-arg", f"BASE_IMAGE={base_image}", "--tag", tag, str(context)], check=True)
        if push:
            subprocess.run(["docker", "push", tag], check=True)
