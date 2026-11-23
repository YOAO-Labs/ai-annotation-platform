"""Use the deployment SQL loader without depending on pytest's working directory."""

from pathlib import Path
from runpy import run_path

_loader = run_path(str(Path(__file__).resolve().parents[3] / "scripts/grant_sql.py"))
read_psql_grant_script = _loader["read_psql_grant_script"]
read_runtime_grant_statements = _loader["read_runtime_grant_statements"]
