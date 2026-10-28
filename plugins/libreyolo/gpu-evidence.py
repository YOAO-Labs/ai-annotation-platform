"""Execute pre-authorized GPU lifecycle scenarios and record real responses.

The controller supplies fresh signed headers in a private scenario file; this
harness does not sign permits or infer success from memory usage alone.
"""
import argparse
import json
from pathlib import Path
import subprocess

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--token-file", type=Path, required=True)
parser.add_argument("--scenario", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
scenario = json.loads(args.scenario.read_text())
steps = scenario["steps"]
allowed = {"/health", "/warmup", "/predict", "/drain", "/drain/cancel", "/unload", "/lifecycle/mode", "/lifecycle/reset"}
if any(step["path"] not in allowed or step["method"] not in {"GET", "POST"} for step in steps):
    raise ValueError("Unsupported evidence action")
if sum(step["path"] == "/predict" for step in steps) < 2 or sum(step["path"] == "/unload" for step in steps) < 2:
    raise ValueError("Two real predict and unload cycles are required")
token = args.token_file.read_text().strip()
if len(token) < 32:
    raise ValueError("Invalid plugin token")


def memory():
    return subprocess.check_output(["nvidia-smi", "--query-gpu=uuid,memory.used,memory.free", "--format=csv,noheader,nounits"], text=True).strip()


results = []
with httpx.Client(base_url=args.url, timeout=300, follow_redirects=False) as client:
    for step in steps:
        headers = {"Authorization": f"Bearer {token}", **step.get("headers", {})}
        before = memory()
        response = client.request(step["method"], step["path"], headers=headers, json=step.get("body"))
        response.raise_for_status()
        results.append({"path": step["path"], "status": response.status_code,
                        "response": response.json(), "gpu_before": before, "gpu_after": memory()})
args.output.write_text(json.dumps({"scenario": scenario.get("name"), "steps": results}, indent=2) + "\n")
print(f"Evidence recorded at {args.output}; review against lifecycle qualification criteria.")
