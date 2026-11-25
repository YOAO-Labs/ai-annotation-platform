from __future__ import annotations

import subprocess
import sys
import uuid

from yoao_inference_contract.preannotate import PipelineStage, PreannotateRequest
from yoao_inference_contract.pipeline import validate_saved_pipeline


def test_shared_saved_pipeline_contract_canonicalizes_task_alias():
    backend_id = uuid.uuid4()
    result = validate_saved_pipeline(
        [
            {
                "stage": 0,
                "ml_backend_id": str(backend_id).upper(),
                "task_type": "detection",
            }
        ]
    )

    assert result == [{"stage": 0, "ml_backend_id": str(backend_id), "task": "detect"}]
    assert (
        PreannotateRequest(
            ml_backend_id=backend_id,
            pipeline_stages=[PipelineStage(stage=0, ml_backend_id=backend_id)],
        ).ml_backend_id
        == backend_id
    )


def test_shared_contract_import_does_not_import_inference_provider():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from yoao_inference_contract import PreannotateRequest; "
            "assert PreannotateRequest.__name__ == 'PreannotateRequest'; "
            "assert 'aap_inference' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
