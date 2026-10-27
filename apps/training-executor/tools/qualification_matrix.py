"""Bounded isolated CPU matrix execution for the existing qualification tools."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from training_contract.models import model_spec


def source_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_matrix(script, manifest, *, timeout=1200, output=None):
    if not 1 <= timeout <= 3600:
        raise ValueError(
            "Each isolated CPU model has a timeout between 1 and 3600 seconds"
        )
    value = json.loads(Path(manifest).read_text())
    if (
        set(value) != {"schema", "models"}
        or value["schema"] != 1
        or not isinstance(value["models"], list)
        or not 1 <= len(value["models"]) <= 34
    ):
        raise ValueError("Expected a nonempty qualification matrix schema 1")
    records, seen = [], set()
    for item in value["models"]:
        if set(item) != {"base_model", "base"}:
            raise ValueError("Each matrix entry requires exact base_model and base")
        framework = (
            "libreyolo"
            if item["base_model"].lower().startswith("libreyolo")
            else "ultralytics"
        )
        spec = model_spec(framework, item["base_model"])
        if spec.id in seen or (
            Path(script).name == "qualify_segmentation_initialization.py"
            and framework != "ultralytics"
        ):
            raise ValueError(
                "Each qualified model appears exactly once in its supported matrix"
            )
        seen.add(spec.id)
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--base",
                    item["base"],
                    "--model",
                    spec.id,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            lines = [
                line
                for line in process.stdout.splitlines()
                if line.startswith("AAP_") and "=" in line
            ]
            if process.returncode or not lines:
                raise RuntimeError(process.stderr[-4000:])
            record = json.loads(lines[-1].split("=", 1)[1])
            if record["base_model"] != spec.id:
                raise ValueError("Observed matrix model identity changed")
            source = Path(item["base"])
            record["input"] = {"imgsz": 128, "batch": 1}
            record["source"] = (
                {
                    "name": source.name,
                    "sha256": source_sha256(source),
                }
                if source.is_file()
                else {"architecture": item["base"]}
            )
            record["cpu_passed"] = True
        except (subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError) as exc:
            record = {"base_model": spec.id, "cpu_passed": False, "reason": str(exc)}
        records.append(record)
        print(
            f"CPU native {spec.id}: {'passed' if record['cpu_passed'] else 'failed'}",
            file=sys.stderr,
            flush=True,
        )
        report = {
            "schema": 1,
            "device": "cpu",
            "complete": len(records) == len(value["models"]),
            "models": records,
        }
        if output is not None:
            Path(output).write_text(
                json.dumps(report, indent=2, allow_nan=False) + "\n"
            )
    if not all(record["cpu_passed"] for record in records):
        raise RuntimeError(
            "One or more native CPU qualifications failed; inspect the per-model report"
        )
    return report
