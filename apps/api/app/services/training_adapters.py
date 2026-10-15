"""Capabilities and fixed options of the qualified training adapters."""

from training_contract import ADAPTERS  # re-export the shared legacy identity


def capabilities(framework):
    adapter = ADAPTERS[framework]
    return {
        "task_types": ["image-detection"],
        "resume": True,
        "artifact_download": True,
        "model_registration": adapter["model_registration"],
    }


def fixed_options(framework):
    common = {
        "device": 0,
        "workers": 2,
        "amp": False,
        "cache": False,
        "deterministic": True,
        "optimizer": "SGD",
    }
    if framework == "libreyolo":
        common.update(
            device="cuda:0",
            eval_interval=1,
            save_period=0,
            save_plots=False,
            cuda_graph=False,
            compile=False,
            allow_download_scripts=False,
            opencv_threads=1,
            torch_threads=4,
            torch_interop_threads=4,
        )
    return common
