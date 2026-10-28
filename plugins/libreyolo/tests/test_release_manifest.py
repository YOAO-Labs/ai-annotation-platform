import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release_manifest", Path(__file__).parents[1] / "release-manifest.py")
release_manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_manifest)


def test_manifest_uses_inspected_console_script():
    entry = ["author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"]
    assert release_manifest.inspected_entrypoint({"Entrypoint": entry, "Cmd": []}) == entry


@pytest.mark.parametrize("entry,cmd", [
    (["/bin/sh", "serve", "--host", "0.0.0.0", "--port", "8000"], []),
    (["author;plugin", "serve", "--host", "0.0.0.0", "--port", "8000"], []),
    (["author-plugin", "serve", "--host", "0.0.0.0", "--port", "9000"], []),
    (["author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"], ["extra"]),
    ("author-plugin serve", []),
])
def test_manifest_rejects_unapproved_entrypoint(entry, cmd):
    with pytest.raises(ValueError):
        release_manifest.inspected_entrypoint({"Entrypoint": entry, "Cmd": cmd})
