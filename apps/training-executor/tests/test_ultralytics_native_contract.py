"""Exercise the pinned trainer loop and offline fonts in fresh CPU processes."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("label", ["shape", "形状"], ids=["ascii", "unicode"])
def test_native_frozen_batch_and_offline_fonts(tmp_path, label):
    readonly = {name: tmp_path / name for name in ("HOME", "XDG_CACHE_HOME")}
    for path in readonly.values():
        path.mkdir(mode=0o555)
    config = tmp_path / "yolo-config"
    try:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), label],
            cwd=tmp_path,
            env={
                **os.environ,
                **{name: str(path) for name, path in readonly.items()},
                "MPLCONFIGDIR": str(readonly["XDG_CACHE_HOME"]),
                "YOLO_CONFIG_DIR": str(config),
                "YOLO_AUTOINSTALL": "0",
                "YOLO_OFFLINE": "1",
                "CUDA_VISIBLE_DEVICES": "",
                "RANK": "-1",
                "LOCAL_RANK": "-1",
                "WORLD_SIZE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(p for p in sys.path if os.path.isabs(p)),
            },
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        assert not any(entry for path in readonly.values() for entry in path.iterdir())
        assert not list(config.rglob("*.ttf")) and not list(config.rglob("*.ttc"))
    finally:
        for path in readonly.values():
            path.chmod(0o755)


def _run_native(label):
    paths = {
        "Arial.ttf": Path(
            os.environ.get(
                "YOAO_ULTRALYTICS_ASCII_FONT",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
        )
        .expanduser()
        .resolve(),
        "Arial.Unicode.ttf": Path(
            os.environ.get(
                "YOAO_ULTRALYTICS_UNICODE_FONT",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            )
        )
        .expanduser()
        .resolve(),
    }
    protected = {*paths.values(), Path.home(), Path(os.environ["XDG_CACHE_HOME"])}
    attempts = []

    def audit(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
            attempts.append(event)
            raise PermissionError(f"Network disabled: {event}")
        if (
            event == "open"
            and isinstance(args[0], (str, bytes))
            and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(path.is_relative_to(root) for root in protected):
                attempts.append(f"write {path}")
                raise PermissionError(f"Read-only font/cache path: {path}")

    sys.addaudithook(audit)
    import torch
    from PIL import Image, ImageDraw
    from ultralytics import YOLO, settings
    from ultralytics.data import utils as data_utils
    from ultralytics.models.yolo.detect import DetectionTrainer
    from ultralytics.utils import downloads
    from ultralytics.utils.plotting import Annotator

    from training_executor.ultralytics_adapter import (
        UltralyticsAttemptTrainer,
        configure_offline_fonts,
    )

    def reject_download(*args, **kwargs):
        attempts.append("download")
        raise AssertionError("No download or online font lookup is allowed")

    downloads.is_url = downloads.safe_download = reject_download
    data_utils.safe_download = reject_download
    settings.update(
        {key: False for key, value in settings.items() if type(value) is bool}
    )
    torch.set_num_threads(2)
    configure_offline_fonts()

    expected_font = "Arial.ttf" if label.isascii() else "Arial.Unicode.ttf"
    selected = []
    resolve_font = data_utils.check_font

    def observe_font(name):
        selected.append(name)
        return resolve_font(name)

    data_utils.check_font = observe_font
    image = Image.new("RGB", (96, 96), "black")
    annotator = Annotator(image, example=label)
    assert Path(annotator.font.path).resolve() == paths[expected_font]
    assert bytes(annotator.font.getmask(label[0])) != bytes(
        annotator.font.getmask("\U0010ffff")
    )
    annotator.box_label([12, 12, 70, 70], label)

    root = Path.cwd()
    for split in ("train", "val"):
        for kind in ("images", "labels"):
            (root / kind / split).mkdir(parents=True)
        for index in range(4 if split == "train" else 2):
            canvas = Image.new("RGB", (96, 96), "black")
            ImageDraw.Draw(canvas).rectangle((24, 24, 71, 71), fill="white")
            canvas.save(root / "images" / split / f"{index}.png")
            (root / "labels" / split / f"{index}.txt").write_text("0 .5 .5 .5 .5\n")
    data = root / "data.yaml"
    data.write_text(
        f"path: {root}\ntrain: images/train\nval: images/val\nnames: ['{label}']\n",
        encoding="utf-8",
    )

    class AttemptTrainer(UltralyticsAttemptTrainer, DetectionTrainer):
        pass

    class Interrupted(Exception):
        pass

    frozen_batch = 4
    options = dict(
        trainer=AttemptTrainer,
        data=str(data),
        batch=frozen_batch,
        device="cpu",
        epochs=2,
        imgsz=96,
        workers=0,
        amp=False,
        pretrained=False,
        deterministic=True,
        optimizer="SGD",
        mosaic=0,
        close_mosaic=0,
        plots=False,
        save=True,
        exist_ok=False,
        name="framework",
    )

    def train(stage, resume=None, *, inject_oom=False, interrupt=False):
        model = YOLO(str(resume) if resume else "yolo11n.yaml", task="detect")
        batches = []
        failure = torch.cuda.OutOfMemoryError("CUDA out of memory (injected)")

        def started(trainer):
            assert trainer.start_epoch == int(resume is not None)
            assert trainer.args.batch == trainer.batch_size == frozen_batch
            assert trainer.train_loader.batch_size == frozen_batch

            def forward(_, args):
                batch = args[0]
                if isinstance(batch, dict):
                    batches.append((trainer.epoch, batch["img"].shape[0]))
                    if inject_oom:
                        raise failure

            trainer.model.register_forward_pre_hook(forward)

        def fitted(trainer):
            if interrupt and trainer.epoch == 0:
                assert trainer.last.is_file()
                raise Interrupted

        model.add_callback("on_train_start", started)
        model.add_callback("on_fit_epoch_end", fitted)
        kwargs = {
            **options,
            "project": str(root / stage),
            "resume": str(resume) if resume else False,
        }
        try:
            if inject_oom:
                with pytest.raises(torch.cuda.OutOfMemoryError) as failure_info:
                    model.train(**kwargs)
                assert failure_info.value is failure
                assert not model.trainer.last.exists()
            elif interrupt:
                with pytest.raises(Interrupted):
                    model.train(**kwargs)
            else:
                model.train(**kwargs)
                assert model.trainer.last.exists()
            assert model.trainer.args.batch == model.trainer.batch_size == frozen_batch
            assert batches == [(int(resume is not None), frozen_batch)]
            return model.trainer.last
        finally:
            for name in ("train_loader", "test_loader"):
                loader = getattr(model.trainer, name, None)
                if loader is not None:
                    loader.close()

    train("cold-oom", inject_oom=True)
    checkpoint = train("initial", interrupt=True)
    assert torch.load(checkpoint, map_location="cpu", weights_only=False)["epoch"] == 0
    train("resumed-oom", resume=checkpoint, inject_oom=True)
    train("resumed", resume=checkpoint)
    assert selected and set(selected) == {expected_font}
    assert not attempts


if __name__ == "__main__":
    _run_native(sys.argv[1])
