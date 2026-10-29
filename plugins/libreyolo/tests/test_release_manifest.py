import pytest

from aap_libreyolo_plugin import manifest


def test_manifest_uses_inspected_console_script():
    entry = ["author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"]
    assert manifest.inspected_entrypoint({"Entrypoint": entry, "Cmd": []}) == entry


@pytest.mark.parametrize(
    "entry,cmd",
    [
        (["/bin/sh", "serve", "--host", "0.0.0.0", "--port", "8000"], []),
        (["author;plugin", "serve", "--host", "0.0.0.0", "--port", "8000"], []),
        (["author-plugin", "serve", "--host", "0.0.0.0", "--port", "9000"], []),
        (["author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"], ["extra"]),
        ("author-plugin serve", []),
    ],
)
def test_manifest_rejects_unapproved_entrypoint(entry, cmd):
    with pytest.raises(ValueError):
        manifest.inspected_entrypoint({"Entrypoint": entry, "Cmd": cmd})
