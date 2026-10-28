from copy import deepcopy
import hashlib
import json

import pytest
from training_contract.model_return import libre_identity

from aap_libreyolo_plugin.install import install_request_model
from aap_libreyolo_plugin.provider import validate_receipt


def receipt():
    classes = ["car", "person"]
    digest = hashlib.sha256(json.dumps(classes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"model_version_id": "c4b2d973-f22e-4218-9e39-9fcfce5fc956", "sha256": "a" * 64,
            "classes_digest": digest, "classes": classes, "framework": "libreyolo",
            "framework_version": "1.6.0", "task": "detection", "artifact_size_bytes": 123,
            **libre_identity("LibreYOLO9t")}


def test_receipt_class_order_and_qualification_are_immutable():
    valid = receipt()
    validate_receipt(valid)
    for changed in ({"classes": ["person", "car"]}, {"model_size": "e"},
                    {"runtime_qualification": {}}, {"artifact_format": "foreign"}):
        broken = deepcopy(valid)
        broken.update(changed)
        with pytest.raises(ValueError):
            validate_receipt(broken)


def test_install_restricts_artifact_origin_and_size():
    request = install_request_model(["https://assets.example"])
    fields = {**receipt(), "size_bytes": 123, "download_url": "https://assets.example/object"}
    request.model_validate(fields)
    for changed in ({"download_url": "http://assets.example/object"},
                    {"download_url": "https://assets.example@evil.example/object"},
                    {"download_url": "https://assets.example/object#fragment"},
                    {"size_bytes": 124}):
        with pytest.raises(ValueError):
            request.model_validate({**fields, **changed})
