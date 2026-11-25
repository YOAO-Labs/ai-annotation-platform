"""Load frozen per-module contract schemas without depending on pytest's working directory."""

from pathlib import Path
from runpy import run_path

_loader = run_path(
    str(Path(__file__).resolve().parents[3] / "scripts/contract_schemas.py")
)
load_contract_schema = _loader["load_contract_schema"]
canonical_contract_bytes = _loader["canonical_contract_bytes"]
contract_schema_sha256 = _loader["contract_schema_sha256"]
