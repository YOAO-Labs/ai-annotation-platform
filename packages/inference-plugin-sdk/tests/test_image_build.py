import pytest

from aap_inference_plugin.image_build import _locked_names, build_image


@pytest.mark.parametrize(
    "line",
    [
        "author-plugin>=1 --hash=sha256:" + "a" * 64,
        "author-plugin==1",
        "-r other.lock",
        "author-plugin @ https://example.com/file.whl --hash=sha256:" + "a" * 64,
    ],
)
def test_lock_rejects_unpinned_or_unhashed_entries(tmp_path, line):
    lock = tmp_path / "lock"
    lock.write_text(line + "\n")
    with pytest.raises(ValueError):
        _locked_names(lock)


@pytest.mark.parametrize("script", ["/bin/sh", "author;touch", "author plugin", ""])
def test_builder_rejects_shell_or_path_entrypoints(tmp_path, script):
    with pytest.raises(ValueError, match="entrypoint"):
        build_image(
            base_image="python@sha256:" + "a" * 64,
            wheelhouse=tmp_path,
            requirements_lock=tmp_path / "requirements.lock",
            entrypoint=script,
            tag="example:tag",
        )
