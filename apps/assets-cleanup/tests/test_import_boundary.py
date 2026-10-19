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
        "pyvips",
        "pillow",
        "aap_assets_images_runtime",
        "aap_assets_media_runtime",
    ):
        assert forbidden not in source.lower()


def test_docker_installs_only_portable_asset_contracts_not_media_runtime_extras():
    root = Path(__file__).resolve().parents[3]
    project = (root / "apps/assets-cleanup/pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (root / "apps/assets-cleanup/Dockerfile").read_text(encoding="utf-8")

    assert '"aap-assets-media"' in project
    assert 'aap-assets-media = { path = "../assets-media" }' in project
    assert "--extra worker" in dockerfile
    assert "--extra runtime" not in dockerfile
    assert "apps/assets-media/src/aap_assets_images/" in dockerfile
    assert "apps/assets-media/src/aap_assets_inputs/" in dockerfile
    assert "apps/assets-media/src/aap_assets_inputs_runtime/" in dockerfile
    assert "aap_assets_images_runtime" not in dockerfile
