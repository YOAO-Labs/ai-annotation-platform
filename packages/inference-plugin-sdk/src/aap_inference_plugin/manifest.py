"""Validate plugin installation intent using the shared inference contract."""
from pathlib import Path

from aap_inference_contract.plugin import PluginManifest


def load_manifest(path: str | Path) -> PluginManifest:
    return PluginManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
