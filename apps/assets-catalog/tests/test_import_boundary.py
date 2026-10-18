import subprocess
import sys
from pathlib import Path


def test_catalog_and_spatial_imports_do_not_pull_api_orm_queue_or_ml_modules() -> None:
    source_dir = Path(__file__).parents[1] / "src"
    workspace_source_dir = (
        Path(__file__).parents[2] / "_shared" / "workspace_contract" / "src"
    )
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(source_dir)!r}); "
        f"sys.path.insert(0, {str(workspace_source_dir)!r}); "
        "import aap_assets_catalog.axis_convention; "
        "import aap_assets_catalog.axis_sniff; "
        "import aap_assets_catalog.spatial_values; "
        "import aap_assets_catalog.api; "
        "forbidden = {'app', 'sqlalchemy', 'celery', 'redis', 'torch'}; "
        "assert not forbidden.intersection(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)
