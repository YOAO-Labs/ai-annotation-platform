"""Contract behavior, drift, dependencies and observed native values."""

from copy import deepcopy
import pytest
from training_contract import definition, resolve, verify_frozen, verify_native
from training_contract.catalog import digest
from training_contract.validation import CANONICAL


def request(framework="ultralytics", **values):
    schema = definition(framework)
    return {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": values,
    }


def frozen(framework="ultralytics", **values):
    configuration = resolve(framework, None, request(framework, **values))
    return {
        "framework": framework,
        "framework_version": configuration["identity"]["framework_version"],
        "adapter_revision": 2,
        "configuration": configuration,
        "classes": ["car"],
        "base_model": {
            "id": "yolo11n" if framework == "ultralytics" else "LibreYOLO9t"
        },
        "parameters": {
            key: configuration["effective_values"][key] for key in CANONICAL
        },
    }


@pytest.mark.parametrize("framework,count", [("ultralytics", 114), ("libreyolo", 92)])
def test_complete_inventory_has_disposition_and_stable_content_identity(
    framework, count
):
    schema = definition(framework)
    assert len(schema["fields"]) == count
    assert len({f["key"] for f in schema["fields"]}) == count
    assert all(
        f["status"] in {"editable", "platform-managed", "inapplicable", "unqualified"}
        and f["reason"]
        for f in schema["fields"]
    )
    expected = schema.pop("schema_digest")
    assert digest(schema) == expected
    changed = definition(framework)
    changed["fields"][0]["default"] = "drift"
    changed.pop("schema_digest")
    assert digest(changed) != expected


@pytest.mark.parametrize(
    "key,value",
    [
        ("epochs", True),
        ("batch", -1),
        ("imgsz", [128, 256]),
        ("imgsz", 129),
        ("lr0", float("nan")),
        ("lr0", "0.01"),
        ("lr0", float("inf")),
        ("device", 1),
        ("callback", "code"),
        ("time", 1),
        ("pose", 1),
        ("freeze", [False]),
    ],
)
def test_unknown_reserved_inapplicable_and_unsafe_values_are_rejected(key, value):
    with pytest.raises(ValueError):
        resolve("ultralytics", None, request(**{key: value}))


def test_native_lists_optional_and_aliases_round_trip_without_precision_loss():
    spec = frozen(
        "libreyolo",
        mosaic_scale=[0.51, 1.47],
        nbs=None,
        lr0=0.0000123456789,
        warmup_lr_start=0.000001,
        mixup=0.25,
    )
    verified = verify_frozen(spec)
    assert verified["effective_values"]["mosaic_scale"] == [0.51, 1.47]
    assert verified["user_values"]["mixup_prob"] == 0.25
    assert spec["parameters"]["lr0"] == 0.0000123456789
    for values in (
        {"mixup": 0.1, "mixup_prob": 0.1},
        {"mosaic_scale": [1.5, 0.5]},
        {"mosaic_scale": [0.5]},
        {"letterbox_pad": "unknown"},
    ):
        with pytest.raises(ValueError):
            resolve("libreyolo", None, request("libreyolo", **values))


@pytest.mark.parametrize(
    "framework,values",
    [
        ("ultralytics", {"rect": True, "mosaic": 0.5}),
        ("ultralytics", {"optimizer": "AdamW", "warmup_momentum": 0.8}),
        ("ultralytics", {"epochs": 2, "warmup_epochs": 3}),
        ("ultralytics", {"warmup_epochs": 0, "warmup_bias_lr": 0.1}),
        ("libreyolo", {"optimizer": "adamw", "momentum": 0.8}),
        ("libreyolo", {"scheduler": "cos", "warmup_momentum": 0.8}),
        ("libreyolo", {"ema": False, "ema_decay": 0.99}),
        ("libreyolo", {"mosaic_prob": 0, "mixup_prob": 0.1}),
        ("libreyolo", {"warmup_lr_start": 0.01, "lr0": 0.001}),
    ],
)
def test_ignored_or_clamped_submissions_fail_instead_of_claiming_effect(
    framework, values
):
    with pytest.raises(ValueError):
        resolve(framework, None, request(framework, **values))


def test_dependency_defaults_and_frozen_integrity_are_explicit():
    spec = frozen(epochs=2, rect=True)
    configuration = spec["configuration"]
    assert configuration["effective_values"]["warmup_epochs"] == 1
    assert configuration["effective_values"]["mosaic"] == 0
    assert configuration["origins"]["mosaic"] == "dependency:rect"
    verify_frozen(spec)
    corrupt = deepcopy(spec)
    corrupt["configuration"]["effective_values"]["lr0"] = 0.02
    with pytest.raises(ValueError, match="mismatch"):
        verify_frozen(corrupt)


def test_native_observation_requires_real_values_and_cannot_override_algorithm_fields():
    spec = frozen(epochs=2)
    runtime = {
        "data": "/state/data.yaml",
        "model": "/models/yolo11n.pt",
        "project": "/state/attempt",
        "name": "framework",
        "exist_ok": False,
        "resume": False,
    }
    observed = {**spec["configuration"]["effective_values"], **runtime}
    assert verify_native(spec, observed, runtime)["verified"]
    observed["optimizer"] = "AdamW"
    with pytest.raises(ValueError, match="optimizer"):
        verify_native(spec, observed, runtime)
    with pytest.raises(ValueError, match="override"):
        verify_native(spec, observed, {**runtime, "optimizer": "AdamW"})
    with pytest.raises(ValueError, match="observation"):
        verify_native(spec, {}, runtime)


def test_historical_task_identities_and_digests_are_immutable():
    import json
    from pathlib import Path
    from training_contract.catalog import (
        definitions,
        schema_identity,
        definition_for_identity,
    )

    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "legacy_task_identities.json").read_text()
    )
    assert [schema_identity(schema) for schema in definitions(legacy=True)] == expected
    for schema in definitions(legacy=True):
        identity = schema_identity(schema)
        base = (
            "yolo11n-seg"
            if identity["task"] == "image-instance-segmentation"
            else (
                "yolo11n" if identity["framework"] == "ultralytics" else "LibreYOLO9t"
            )
        )
        # The frozen identity selects its exact historical schema before validation.
        assert (
            schema_identity(
                definition_for_identity(identity["framework"], base, identity)
            )
            == identity
        )


def test_optional_train_evaluation_and_random_initialization_are_frozen_separately():
    from training_contract.validation import native_values

    default = frozen()
    assert default["configuration"]["effective_values"]["pretrained"] is True
    assert default["configuration"]["effective_values"]["evaluate_train"] is False
    config = frozen(pretrained=False, evaluate_train=True)
    resolved = verify_frozen(config)
    assert resolved["effective_values"]["pretrained"] is False
    assert resolved["effective_values"]["evaluate_train"] is True
    assert "evaluate_train" not in native_values(resolved)
    with pytest.raises(ValueError, match="初始化"):
        verify_frozen({**config, "initialization": {"kind": "trained_model"}})


@pytest.mark.parametrize(
    "base,expected",
    [
        ("yolo11n", "3d04d5bdad575864f7773b35441f30d14b608cc63bc5c779c36399e50d293232"),
        (
            "yolo11n-seg",
            "4e0b42e370210e2dc0acd0291221a1af50ae2b40991c9d602a936c6e99c6fecd",
        ),
    ],
)
def test_prior_parameter_schema_is_still_exactly_resolvable(base, expected):
    from training_contract.catalog import definition_for_identity, schema_identity

    prior = definition("ultralytics", base, revision=3)
    assert prior["schema_digest"] == expected
    assert definition_for_identity("ultralytics", base, schema_identity(prior)) == prior
    resolved = resolve(
        "ultralytics",
        base,
        {
            "schema_id": prior["schema_id"],
            "schema_digest": prior["schema_digest"],
            "values": {},
        },
    )
    assert "evaluate_train" not in resolved["effective_values"]
    assert resolved["effective_values"]["pretrained"] is True


@pytest.mark.parametrize(
    "base,expected",
    [
        ("yolo11n", "2fa8760d69bc97f3ce5a042163a2f3e50b419159ce8dfabcd8ec86f7f042c5fa"),
        (
            "yolo11n-seg",
            "79ecff81a2dac4071aadd78a0205ee02f2abe5f58190f5ba0dd89e71dc84d5e8",
        ),
    ],
)
def test_previous_current_schema_and_version_remain_frozen(base, expected):
    schema = definition("ultralytics", base, version="8.4.177")
    assert schema["schema_digest"] == expected
    assert schema["framework_version"] == "8.4.177"
    assert schema["schema_id"].endswith("-4")
