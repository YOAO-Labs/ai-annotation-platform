"""Use the deployment SQL loader without depending on pytest's working directory."""

from pathlib import Path
from runpy import run_path

read_psql_grant_script = run_path(
    str(Path(__file__).resolve().parents[3] / "scripts/grant_sql.py")
)["read_psql_grant_script"]
