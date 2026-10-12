"""Independently check acquisition identities and saved-cloud provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from omega_densification import error_summary, project, transform
from qualify_vggt_omega import digest
from vggt_common import VIEWER_FROM_REFERENCE, verify_runtime_sources, write_json


def audit(output):
    protocol = json.loads((output / "protocol.json").read_text())
    inference = json.loads((output / "inference.json").read_text())
    verification = json.loads((output / "inference-verification.json").read_text())
    source = json.loads((output / "verification-source.json").read_text())
    consolidated = verify_runtime_sources(output)
    paths = {}
    for run in protocol["runs"]:
        if (
            run["target_scan"] in run["anchor_scans"]
            or run["target_scan"] == run["single_scan"]
        ):
            raise ValueError("heldout acquisition leaked into an allowed set")
        for row in [*run["scans"], *run["views"]]:
            if row["path"] in paths and paths[row["path"]] != row["sha256"]:
                raise ValueError("inconsistent frozen input hashes")
            paths[row["path"]] = row["sha256"]
    for path, expected in paths.items():
        if digest(path) != expected:
            raise ValueError(f"sensor input changed, including scoring LiDAR: {path}")
    for name, expected in source["source_hashes"].items():
        if (
            digest(output / "source-snapshot" / name) != expected
            or not consolidated
            and digest(Path(__file__).with_name(name)) != expected
        ):
            raise ValueError(
                f"final executable source changed after exact reproduction: {name}"
            )
    for record in inference["runs"]:
        if (
            record["status"] != "completed"
            or digest(output / "predictions" / f"{record['id']}.npz")
            != record["prediction_sha256"]
        ):
            raise ValueError("prediction missing or replaced")
    if len(verification["runs"]) != len(protocol["runs"]) or not all(
        r["status"] == "completed"
        and all(
            a["exact"] and a["max_abs"] == 0 for a in r["array_verification"].values()
        )
        for r in verification["runs"]
    ):
        raise ValueError("model exact reproduction did not complete")
    rng = np.random.default_rng(97531)
    rows = []
    paired = {}
    range_rows = []
    for run in protocol["runs"]:
        directory = output / "runs" / run["id"]
        metrics = json.loads((directory / "metrics.json").read_text())
        targets = np.load(directory / "targets.npz")
        if len(np.unique(targets["raw_rows"])) != len(targets["raw_rows"]):
            raise ValueError("same scoring return counted more than once")
        key = (run["scene"], run["centre"])
        stable_targets = {
            name: targets[name].copy()
            for name in ("raw_rows", "uv", "truth_z", "static", "actor", "edge")
        }
        stable_targets["camera_tokens"] = np.array(
            [run["views"][int(i)]["token"] for i in targets["view"]]
        )
        if key in paired:
            for name in stable_targets:
                if not np.array_equal(stable_targets[name], paired[key][name]):
                    raise ValueError(
                        f"window sizes changed the target contract: {key} {name}"
                    )
        else:
            paired[key] = stable_targets
        reference_xyz = transform(
            targets["points"], np.asarray(run["reference_from_global"])
        )
        common_range = targets["static"] & (np.linalg.norm(reference_xyz, axis=1) <= 80)
        with np.load(directory / "scored-depths.npz") as scored:
            range_rows.append(
                {
                    "window": run["window"],
                    "split": run["split"],
                    "truth": targets["truth_z"].copy(),
                    "mask": common_range,
                    "excluded": int((targets["static"] & ~common_range).sum()),
                    "predictions": {k: scored[k].copy() for k in scored.files},
                }
            )
        targets.close()
        with np.load(directory / "corrected-depths.npz") as cached:
            corrected = {k: cached[k].copy() for k in ("scaled", "corrected")}
        with np.load(output / "predictions" / f"{run['id']}.npz") as cached:
            predicted_rgb = cached["rgb"].copy()
        point_count, tested = 0, 0
        maximum_depth_error = maximum_pixel_error = maximum_measured_error = 0.0
        for method, asset in metrics["clouds"].items():
            path = output / asset["url"]
            if path.stat().st_size != asset["counts"]["total"] * 16:
                raise ValueError("saved point buffer length mismatch")
            dtype = np.dtype([("xyz", "<f4", 3), ("rgb", "u1", 3), ("padding", "u1")])
            buffer = np.fromfile(path, dtype=dtype)
            if not np.isfinite(buffer["xyz"]).all():
                raise ValueError("nonfinite viewer coordinates")
            provenance = np.load(directory / f"{method}-provenance.npz")
            count = len(buffer)
            if any(len(provenance[name]) != count for name in provenance.files):
                raise ValueError("source attributes detached from point geometry")
            kinds = np.fromfile(output / asset["kind_url"], dtype=np.uint8)
            if not np.array_equal(kinds, provenance["kind"]):
                raise ValueError("viewer layer differs from source layer")
            point_count += count
            if method == "heldout_oracle":
                provenance.close()
                continue
            dynamic = np.flatnonzero((kinds == 2) & (provenance["source"] >= 0))
            if any(
                run["views"][int(i)]["frame"] != run["centre"]
                for i in np.unique(provenance["source"][dynamic])
            ):
                raise ValueError("noncentre model actor entered output")
            if method == "fused" and ((kinds == 2) & (provenance["source"] < 0)).any():
                raise ValueError(
                    "another scan's dynamic points entered fused actor layer"
                )
            generated = np.flatnonzero(provenance["source"] >= 0)
            samples = rng.choice(generated, min(1000, len(generated)), replace=False)
            global_from_reference = np.linalg.inv(
                np.asarray(run["reference_from_global"])
            )
            xyz = transform(
                buffer["xyz"][samples] @ VIEWER_FROM_REFERENCE, global_from_reference
            )
            for i in np.unique(provenance["source"][samples]):
                selected = provenance["source"][samples] == i
                indices = samples[selected]
                view = run["views"][int(i)]
                pixels = provenance["pixel"][indices]
                uv = np.column_stack((pixels % 560, pixels // 560))
                got_uv, got_z = project(
                    xyz[selected],
                    np.asarray(view["intrinsic"]),
                    np.asarray(view["global_from_sensor"]),
                )
                depth_name = "scaled" if method == "scale_model" else "corrected"
                expected = corrected[depth_name][int(i), uv[:, 1], uv[:, 0]]
                pixel_error = float(np.max(np.linalg.norm(got_uv - uv, axis=1)))
                depth_error = float(np.max(np.abs(got_z - expected)))
                maximum_pixel_error = max(maximum_pixel_error, pixel_error)
                maximum_depth_error = max(maximum_depth_error, depth_error)
                if pixel_error > 0.1 or depth_error > 0.002:
                    raise ValueError(
                        "saved point no longer matches source pixel and corrected depth"
                    )
                if not np.array_equal(
                    buffer["rgb"][indices], predicted_rgb[int(i), uv[:, 1], uv[:, 0]]
                ):
                    raise ValueError("saved RGB does not match source exposure")
            measured = np.flatnonzero(provenance["source"] < 0)
            samples = rng.choice(measured, min(200, len(measured)), replace=False)
            xyz = transform(
                buffer["xyz"][samples] @ VIEWER_FROM_REFERENCE, global_from_reference
            )
            for negative in np.unique(provenance["source"][samples]):
                scan = run["scans"][-int(negative) - 1]
                if scan["token"] == run["target_scan"]:
                    raise ValueError("heldout real point entered an evaluated cloud")
                selected = provenance["source"][samples] == negative
                raw = np.fromfile(scan["path"], dtype="<f4").reshape(-1, 5)[:, :3]
                expected = transform(
                    raw[provenance["pixel"][samples[selected]]],
                    np.asarray(scan["global_from_sensor"]),
                )
                measured_error = float(
                    np.max(np.linalg.norm(expected - xyz[selected], axis=1))
                )
                maximum_measured_error = max(maximum_measured_error, measured_error)
                if measured_error > 0.002:
                    raise ValueError(
                        "measured cloud point identity or position changed"
                    )
            tested += min(1000, len(generated)) + len(samples)
            provenance.close()
        rows.append(
            {
                "id": run["id"],
                "points_checked_finite": point_count,
                "source_samples_reconstructed": tested,
                "max_pixel_error": maximum_pixel_error,
                "max_depth_error_m": maximum_depth_error,
                "max_measured_position_error_m": maximum_measured_error,
            }
        )
    range_control = {}
    for split in ("all", "development", "validation"):
        range_control[split] = {}
        for window in (3, 5):
            selected = [
                r
                for r in range_rows
                if r["window"] == window and (split == "all" or r["split"] == split)
            ]
            truth = np.concatenate([r["truth"] for r in selected])
            mask = np.concatenate([r["mask"] for r in selected])
            range_control[split][str(window)] = {
                "static_targets_outside_reference_80m": sum(
                    r["excluded"] for r in selected
                ),
                "methods": {
                    method: error_summary(
                        np.concatenate([r["predictions"][method] for r in selected]),
                        truth,
                        mask,
                    )
                    for method in selected[0]["predictions"]
                },
            }
    snapshot = output / (
        "code-consolidation-audit-source" if consolidated else "source-snapshot"
    )
    snapshot.mkdir(exist_ok=True)
    (snapshot / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    write_json(
        output
        / (
            "artifact-audit-consolidated.json"
            if consolidated
            else "artifact-audit.json"
        ),
        {
            "passed": True,
            "distinct_sensor_files_verified": len(paths),
            "windows": len(rows),
            "unique_target_scans": len(paired),
            "runs": rows,
            "audit_source_sha256": digest(__file__),
            "prediction_reproduction": "all arrays bitwise equal",
            "source_check": "historical exact-array proof plus consolidated source fence"
            if consolidated
            else "original executable source",
            "range_control": range_control,
        },
    )
    print(
        json.dumps(
            {
                "passed": True,
                "windows": len(rows),
                "sensor_files": len(paths),
                "points": sum(r["points_checked_finite"] for r in rows),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    audit(parser.parse_args().output)
