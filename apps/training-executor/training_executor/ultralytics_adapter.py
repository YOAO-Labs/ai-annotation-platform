"""Fixed-batch and offline-font boundaries for the pinned Ultralytics runtime."""

import os
from pathlib import Path

from training_contract.observability import (
    NativeConfigurationMismatch,
    NativeRuntimeMismatch,
)


_QUALIFIED_VERSION = "8.4.183"
_FIRST_EPOCH_OOM_RETRY_LIMIT = 3
_FONT_PATHS = {
    "Arial.ttf": (
        "YOAO_ULTRALYTICS_ASCII_FONT",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "Arial.Unicode.ttf": (
        "YOAO_ULTRALYTICS_UNICODE_FONT",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ),
}


def _require_qualified_runtime():
    from ultralytics import __version__

    if __version__ != _QUALIFIED_VERSION:
        raise NativeRuntimeMismatch(
            f"Ultralytics training adapter requires {_QUALIFIED_VERSION}, got {__version__}"
        )


def configure_offline_fonts():
    """Resolve the native dataset and annotation lookups without any cache or network."""
    _require_qualified_runtime()
    from ultralytics.data import utils as data_utils
    from ultralytics.utils import checks, plotting

    fonts = {}
    for name, (variable, default) in _FONT_PATHS.items():
        path = Path(os.environ.get(variable, default)).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.R_OK):
            raise NativeConfigurationMismatch(
                f"{variable} must point to an installed local font file: {path}"
            )
        fonts[name] = path

    def local_font(font="Arial.ttf"):
        try:
            return fonts[font]
        except KeyError as exc:
            raise NativeConfigurationMismatch(
                f"Unsupported Ultralytics training font: {font}"
            ) from exc

    checks.check_font = data_utils.check_font = plotting.check_font = local_font


class UltralyticsAttemptTrainer:
    """Bind a native trainer subclass to the attempt's batch and output directory."""

    def check_resume(self, overrides):
        _require_qualified_runtime()
        frozen_batch = overrides.get("batch")
        if type(frozen_batch) is not int or frozen_batch < 1:
            raise NativeConfigurationMismatch(
                "Ultralytics requires a fixed integer batch"
            )
        super().check_resume(overrides)
        if self.args.batch != frozen_batch:
            raise NativeConfigurationMismatch("Ultralytics changed the frozen batch")
        if self.resume:
            for field in ("project", "name", "data", "exist_ok"):
                setattr(self.args, field, overrides[field])
            self.args.save_dir = str(Path(overrides["project"]) / overrides["name"])

    def run_callbacks(self, event):
        if event == "on_train_epoch_start" and self.epoch == self.start_epoch:
            self._oom_retries = _FIRST_EPOCH_OOM_RETRY_LIMIT
        super().run_callbacks(event)
