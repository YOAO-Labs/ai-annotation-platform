from pathlib import Path
import tomllib


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
    project = tomllib.loads(
        (root / "apps/assets-cleanup/pyproject.toml").read_text(encoding="utf-8")
    )
    dockerfile = (root / "apps/assets-cleanup/Dockerfile").read_text(encoding="utf-8")

    assert "aap-assets-media" in project["project"]["optional-dependencies"]["worker"]
    assert (
        project["tool"]["uv"]["sources"]["aap-assets-media"]["path"]
        == "../assets-media"
    )
    assert "--extra worker" in dockerfile
    assert "--extra runtime" not in dockerfile
    assert "apps/assets-media/src/aap_assets_images/" in dockerfile
    assert "apps/assets-media/src/aap_assets_inputs/" in dockerfile
    assert "apps/assets-media/src/aap_assets_inputs_runtime/" in dockerfile
    assert "apps/assets-media/src/aap_assets_media/contract.py" in dockerfile
    assert "apps/assets-media/src/aap_assets_video_inputs/" in dockerfile
    for module in ("__init__.py", "database.py", "garbage.py", "storage.py"):
        assert (
            f"apps/assets-media/src/aap_assets_video_inputs_runtime/{module}"
            in dockerfile
        )
    assert (
        "apps/assets-media/src/aap_assets_video_inputs_runtime/worker.py"
        not in dockerfile
    )
    assert (
        "apps/assets-media/src/aap_assets_video_inputs_runtime/processing.py"
        not in dockerfile
    )
    assert "aap_assets_images_runtime" not in dockerfile
    assert "accepted-video-inputs/*" in (
        root / "apps/assets-cleanup/README.md"
    ).read_text(encoding="utf-8")
    image_deleter = (
        root / "apps/assets-cleanup/src/aap_assets_cleanup/object_store.py"
    ).read_text(encoding="utf-8")
    assert "accepted-video-inputs" not in image_deleter
