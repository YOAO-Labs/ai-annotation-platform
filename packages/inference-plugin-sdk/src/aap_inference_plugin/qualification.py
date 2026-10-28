"""Run bounded, explicitly local-exclusive LibreYOLO and lifecycle qualification."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from aap_protocol_v2.lifecycle import AdmissionScope, AdmissionTokenClaims, encode_ed25519_public_key, sign_admission_token

from .check import _data_url, check_http, load_fixtures


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _validate_native(report: dict, device: str, gpu_uuid: str) -> None:
    checks = ("missing_weights", "wrong_device", "gpu_residency", "native_multi", "native_empty", "gpu_release")
    if (report.get("mode") != "local-isolated" or report.get("device") != device
            or str(report.get("gpu_uuid", "")).lower() != gpu_uuid.lower()
            or report.get("platform_managed_qualified") is not False):
        raise ValueError("native probe did not attest the requested isolated device and scope")
    facts = report.get("checks")
    if not isinstance(facts, dict):
        raise ValueError("native probe omitted its checks")
    for name in checks:
        entry = facts.get(name)
        if not isinstance(entry, dict) or entry.get("passed") is not True or not entry.get("detail"):
            raise ValueError(f"native probe {name} did not provide passing evidence")


def _sign(key, boot, registry, resource, scope, *, generation=None, epoch="2", operation="local-check"):
    control = scope in {AdmissionScope.MODE, AdmissionScope.RESET, AdmissionScope.DRAIN, AdmissionScope.UNLOAD}
    claims = AdmissionTokenClaims(
        backend_registry_id=registry, gpu_resource_id=resource, boot_id=boot,
        generation=generation, control_epoch=epoch, scope=scope,
        jti=uuid.uuid4().hex, exp=int(time.time()) + 300,
        owner="local-qualification" if control else None,
        operation=operation if control else None,
    )
    return sign_admission_token(claims, private_key=key, kid="local")


async def _lifecycle(url: str, token: str, key, registry: str, resource: str, image: Path) -> dict:
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=120, trust_env=False) as client:
        initial = (await client.get("/health")).json()["residency"]
        boot = initial["boot_id"]
        # HTTP fixture checking used legacy execution; signed reset establishes cold ownership.
        reset_token = _sign(key, boot, registry, resource, AdmissionScope.RESET, epoch="1")
        reset = await client.post("/lifecycle/reset", headers={"X-AAP-GPU-Admission-Token": reset_token}, json={"control_epoch": "1"})
        reset.raise_for_status()
        exclusive = await client.get("/_local/gpu-exclusivity")
        exclusive.raise_for_status()
        if exclusive.json().get("exclusive_sample") is not True:
            raise ValueError("could not establish local GPU process exclusivity")
        baseline_response = await client.get("/_local/gpu-memory")
        baseline_response.raise_for_status()
        baseline = baseline_response.json()
        mode_token = _sign(key, boot, registry, resource, AdmissionScope.MODE)
        mode = await client.post("/lifecycle/mode", headers={"X-AAP-GPU-Admission-Token": mode_token}, json={"gate": "enforce", "control_epoch": "2"})
        mode.raise_for_status()
        source = _data_url(image)
        if len(source) > 3 * 1024 * 1024:
            raise ValueError("local cancellation fixture must be at most 2 MiB; use a smaller representative image")
        tasks = [{"id": i, "file_path": source} for i in range(16)]
        prediction_headers = {"X-AAP-GPU-Generation": "1", "X-AAP-GPU-Admission-Token": _sign(key, boot, registry, resource, AdmissionScope.PREDICT, generation="1")}
        prediction = asyncio.create_task(client.post("/predict", headers=prediction_headers, json={"tasks": tasks, "context": {"task": "detect"}}))
        try:
            deadline = time.monotonic() + 90
            observed = False
            while time.monotonic() < deadline:
                if prediction.done():
                    raise ValueError("prediction ended before a live borrower could be observed; use a slower or larger fixture")
                health = (await client.get("/health")).json()["residency"]
                if health["active_requests"] > 0 and health["borrowers"] > 0:
                    observed = True
                    break
                await asyncio.sleep(0.01)
            if not observed:
                raise ValueError("timed out waiting for native inference borrower")
            prediction.cancel()
            try:
                await prediction
            except asyncio.CancelledError:
                pass
            drain_token = _sign(key, boot, registry, resource, AdmissionScope.DRAIN, generation="2")
            drain_headers = {"X-AAP-GPU-Generation": "2", "X-AAP-GPU-Admission-Token": drain_token}
            drain = await client.post("/drain", headers=drain_headers, json={"generation": "2"})
            drain.raise_for_status()
            live = drain.json()
            if live.get("ready_to_unload") is not False or live.get("active_requests", 0) < 1:
                raise ValueError("drain did not observe active native work after cancellation; repeat with a slower fixture")
            denied = await client.post("/warmup", headers={"X-AAP-GPU-Generation": "2", "X-AAP-GPU-Admission-Token": _sign(key, boot, registry, resource, AdmissionScope.WARMUP, generation="2")}, json={"task": "detect"})
            if denied.status_code < 400:
                raise ValueError("draining service admitted a new workload")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                response = await client.post("/drain", headers=drain_headers, json={"generation": "2"})
                response.raise_for_status()
                if response.json().get("ready_to_unload") is True:
                    break
                await asyncio.sleep(0.05)
            else:
                raise ValueError("native inference did not exit after cancellation; backend remains draining")
            unload_token = _sign(key, boot, registry, resource, AdmissionScope.UNLOAD, generation="2")
            unloaded = await client.post("/unload", headers={"X-AAP-GPU-Generation": "2", "X-AAP-GPU-Admission-Token": unload_token}, json={"generation": "2"})
            unloaded.raise_for_status()
            cold = (await client.get("/health")).json()["residency"]
            if cold.get("gpu_loaded") is not False or cold.get("pools", {}).get("models", {}).get("resident") is not False or cold.get("active_requests") != 0:
                raise ValueError("unload did not establish a trusted cold models pool")
            exclusive = await client.get("/_local/gpu-exclusivity")
            exclusive.raise_for_status()
            if exclusive.json().get("exclusive_sample") is not True:
                raise ValueError("GPU process exclusivity was lost during qualification")
            memory_response = await client.get("/_local/gpu-memory")
            memory_response.raise_for_status()
            after = memory_response.json()
            if (after["allocated_bytes"] > baseline["allocated_bytes"] or
                    after["reserved_bytes"] > baseline["reserved_bytes"]):
                raise ValueError("CUDA allocations or reservations remain above the cold baseline after cancellation and unload")
            return {"passed": True, "detail": "observed real borrower, cancelled HTTP, signed drain blocked new work until native exit, then signed unload established cold residency and allocator baseline", "drain": live, "cold": cold, "memory_before": baseline, "memory_after": after}
        finally:
            if not prediction.done():
                prediction.cancel()
                try:
                    await prediction
                except asyncio.CancelledError:
                    pass


def qualify_local(fixtures: Path, device: str, gpu_uuid: str, asset_dir: Path, report_path: Path) -> dict:
    if not device.startswith("cuda:") or not device[5:].isdigit() or not gpu_uuid.startswith("GPU-"):
        raise ValueError("local GPU qualification requires --device cuda:<ordinal> and an exact --gpu-uuid")
    _, paths = load_fixtures(fixtures)
    if not 0 < paths["multi"].stat().st_size <= 2 * 1024 * 1024:
        raise ValueError("local cancellation fixture must be a nonempty image no larger than 2 MiB")
    with tempfile.TemporaryDirectory(prefix="aap-plugin-check-") as tmp:
        root = Path(tmp)
        native_path = root / "native.json"
        command = ["aap-libreyolo", "probe", "--device", device, "--gpu-uuid", gpu_uuid,
                   "--asset-dir", str(asset_dir), "--multi-image", str(paths["multi"]),
                   "--empty-image", str(paths["empty"]), "--output", str(native_path)]
        probe = subprocess.run(command, check=False, timeout=900)
        if not native_path.is_file():
            raise ValueError(f"native GPU probe exited {probe.returncode} without a report; inspect probe diagnostics")
        native = json.loads(native_path.read_text(encoding="utf-8"))
        if probe.returncode:
            report_path.write_text(json.dumps({"mode": "local-isolated", "native": native,
                                               "platform_managed_qualified": False}, indent=2) + "\n", encoding="utf-8")
            failures = [name for name, result in native.get("checks", {}).items() if result.get("passed") is not True]
            raise ValueError(f"native GPU probe failed ({', '.join(failures) or 'unknown'}); details in {report_path}")
        _validate_native(native, device, gpu_uuid)
        key = Ed25519PrivateKey.generate()
        token = secrets.token_urlsafe(40)
        token_file, keyring = root / "token", root / "keyring.json"
        token_file.write_text(token, encoding="utf-8")
        keyring.write_text(json.dumps({"local": encode_ed25519_public_key(key.public_key())}), encoding="utf-8")
        os.chmod(token_file, 0o600)
        registry, resource = str(uuid.uuid4()), "local-qualification/" + gpu_uuid
        port = _port()
        url = f"http://127.0.0.1:{port}"
        command = [sys.executable, "-m", "aap_inference_plugin._qualification_server", "--device", device,
                   "--gpu-uuid", gpu_uuid, "--asset-dir", str(asset_dir), "--token-file", str(token_file),
                   "--keyring-file", str(keyring), "--registry", registry, "--resource", resource, "--port", str(port)]
        with (root / "server.log").open("w+") as log:
            environment = {k: v for k, v in os.environ.items() if not k.startswith(("AAP_ROUTE_", "AAP_PLUGIN_"))}
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
            try:
                deadline = time.monotonic() + 60
                with httpx.Client(timeout=2, trust_env=False) as client:
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            log.seek(0)
                            raise ValueError("isolated plugin server exited: " + log.read()[-4000:])
                        try:
                            if client.get(url + "/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise ValueError("isolated plugin server did not become ready")
                http = check_http(url, token, fixtures)
                cancellation = asyncio.run(_lifecycle(url, token, key, registry, resource, paths["multi"]))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        report = {"mode": "local-isolated", "device": device, "gpu_uuid": gpu_uuid,
                  "native": native, "http": http, "cancellation_drain": cancellation,
                  "platform_managed_qualified": False}
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report
