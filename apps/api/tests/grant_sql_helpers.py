from pathlib import Path
import sys


def read_grant_script(path: Path) -> str:
    """Resolve checked-in includes before sending test installer SQL to container stdin."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    try:
        from grant_sql import read_psql_grant_script
    finally:
        sys.path.pop(0)
    return read_psql_grant_script(path)
