from pathlib import Path


def test_worker_package_does_not_import_the_monolith_or_other_runtime_stacks():
    package = Path(__file__).resolve().parents[1] / "src" / "aap_assets_cleanup"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in package.glob("*.py")
    )

    for forbidden in (
        "from app",
        "import app",
        "sqlalchemy",
        "celery",
        "redis",
        "torch",
        "fastapi",
    ):
        assert forbidden not in source.lower()
