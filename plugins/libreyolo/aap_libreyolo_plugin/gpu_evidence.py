"""Execute pre-authorized GPU lifecycle scenarios and record real responses.

The controller supplies fresh signed headers in a private scenario file; this
harness does not sign permits or infer success from memory usage alone.
"""
import asyncio
import argparse
import json
from pathlib import Path
import subprocess

import httpx
from aap_protocol_v2.lifecycle import DrainTransitionResponse, ManagedUnloadResponse

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--token-file", type=Path, required=True)
parser.add_argument("--scenario", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
scenario = json.loads(args.scenario.read_text())
steps = scenario["steps"]
allowed = {"/health", "/warmup", "/predict", "/drain", "/drain/cancel", "/unload", "/lifecycle/mode", "/lifecycle/reset"}
if any((step.get("kind") != "cancel_drain" and
        (step["path"] not in allowed or step["method"] not in {"GET", "POST"}))
       for step in steps):
    raise ValueError("Unsupported evidence action")
if sum(step.get("path") == "/predict" for step in steps) < 2 or sum(step.get("path") == "/unload" for step in steps) < 2:
    raise ValueError("Two real predict and unload cycles are required")
if not any(step.get("kind") == "cancel_drain" for step in steps):
    raise ValueError("A real cancellation/drain observation is required")
token = args.token_file.read_text().strip()
if len(token) < 32:
    raise ValueError("Invalid plugin token")


def memory():
    return subprocess.check_output(["nvidia-smi", "--query-gpu=uuid,memory.used,memory.free", "--format=csv,noheader,nounits"], text=True).strip()


async def cancel_drain(step):
    """Observe a real borrowed native request, disconnect it, then drain."""
    auth = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=args.url, timeout=300, follow_redirects=False) as client:
        prediction = asyncio.create_task(client.post(
            "/predict", headers={**auth, **step["predict_headers"]},
            json=step["predict_body"]))
        observed = None
        try:
            for _ in range(1000):
                health = await client.get("/health")
                health.raise_for_status()
                observed = health.json()
                if observed["residency"]["active_requests"] > 0 and observed["pool"]["borrowers"] > 0:
                    break
                if prediction.done():
                    raise RuntimeError("Prediction completed before a native borrower could be observed")
                await asyncio.sleep(0.005)
            else:
                raise RuntimeError("Timed out waiting for an active native borrower")
            prediction.cancel()
            try:
                await prediction
            except asyncio.CancelledError:
                pass
            response = await client.post("/drain", headers={**auth, **step["drain_headers"]},
                                         json=step["drain_body"])
            response.raise_for_status()
            drain = DrainTransitionResponse.model_validate(response.json()).model_dump(mode="json")
            if not drain["draining"] or drain["ready_to_unload"] or drain["active_requests"] < 1:
                raise RuntimeError("Drain did not observe the cancelled native execution as active")
            for _ in range(1000):
                health = await client.get("/health")
                health.raise_for_status()
                finished = health.json()
                if (finished["residency"]["active_requests"] == 0 and
                    finished["pool"]["borrowers"] == 0 and finished["pool"]["builders"] == 0):
                    return {"observed_before_cancel": observed, "drain": drain,
                            "observed_after_native_exit": finished}
                await asyncio.sleep(0.01)
            raise RuntimeError("Native execution did not drain before deadline")
        finally:
            if not prediction.done():
                prediction.cancel()
                try:
                    await prediction
                except asyncio.CancelledError:
                    pass


results = []
with httpx.Client(base_url=args.url, timeout=300, follow_redirects=False) as client:
    for step in steps:
        headers = {"Authorization": f"Bearer {token}", **step.get("headers", {})}
        before = memory()
        if step.get("kind") == "cancel_drain":
            evidence = asyncio.run(cancel_drain(step))
            results.append({"kind": "cancel_drain", "response": evidence,
                            "gpu_before": before, "gpu_after": memory()})
            continue
        response = client.request(step["method"], step["path"], headers=headers, json=step.get("body"))
        response.raise_for_status()
        if step["path"] == "/unload":
            unload = ManagedUnloadResponse.model_validate(response.json())
            if not unload.unloaded:
                raise RuntimeError("Managed unload did not confirm a fully unloaded state")
        results.append({"path": step["path"], "status": response.status_code,
                        "response": response.json(), "gpu_before": before, "gpu_after": memory()})
args.output.write_text(json.dumps({"scenario": scenario.get("name"), "steps": results}, indent=2) + "\n")
print(f"Evidence recorded at {args.output}; review against lifecycle qualification criteria.")
