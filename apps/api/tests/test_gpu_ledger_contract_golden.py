"""Frozen public contract for the GPU arbitration Redis ledger."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import inspect
import json

from aap_inference.gpu import ledger


_GOLDEN_SHA256 = {
    "constants": "660712e4d720aab6d2cebadda23f403c346635b36ac0967c10406f0723369609",
    "dataclasses": "b57602b0d6783296c48f064565386b21c35bab44d85769603146a02dfadadd79",
    "enums": "fdbb92790ce584dfccc840ce9af67cb29a5c8df4b2d6b96fb6f9ce87ef5312df",
    "keys": "182bc6828e55e7a245bbc0c1b0f9dc554f11bfe176ffbe1503559105a9f36079",
    "signatures": "d67702f5484eb2d8472c3ae5d4ad7fd43fbb7d131988189fa459606a5b72682a",
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _dataclass_params(value: type) -> dict[str, object]:
    """Return the observable dataclass contract across Python 3.11 and 3.12.

    Python 3.11's private ``_DataclassParams`` object does not expose the
    ``match_args``, ``kw_only``, ``slots`` or ``weakref_slot`` decorator flags.
    Freeze their public effects instead: constructor shape is captured beside
    the fields, ``__match_args__`` and ``__slots__`` are visible class contract,
    and per-field ``kw_only`` is recorded below.
    """

    params = value.__dataclass_params__
    stable_names = (
        "init",
        "repr",
        "eq",
        "order",
        "unsafe_hash",
        "frozen",
    )
    slots = value.__dict__.get("__slots__")
    if isinstance(slots, str):
        normalized_slots: list[str] | None = [slots]
    elif slots is None:
        normalized_slots = None
    else:
        normalized_slots = list(slots)
    return {
        **{name: bool(getattr(params, name)) for name in stable_names},
        "match_args": list(value.__dict__.get("__match_args__", ())),
        "slots": normalized_slots,
        "weakref_slot": bool(normalized_slots and "__weakref__" in normalized_slots),
    }


def _default_factory_name(factory: object) -> str:
    module = getattr(factory, "__module__", None)
    qualname = getattr(factory, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    return repr(factory)


def _store_signatures() -> dict[str, object]:
    store = ledger.GPUArbiterStore
    signatures: dict[str, object] = {
        "GPUArbiterStore": {
            "kind": "class",
            "signature": str(inspect.signature(store)),
        }
    }
    public_dunders = {"__init__", "__aenter__", "__aexit__"}
    for name, descriptor in store.__dict__.items():
        # This digest freezes the pre-installation-drain API; the new protocol is
        # covered by its own Redis behavior tests rather than changing the baseline.
        if name in {
            "begin_installation_drain",
            "finish_installation_drain",
            "release_installation_drain",
        }:
            continue
        if name.startswith("_") and name not in public_dunders:
            continue
        target = getattr(store, name)
        if not callable(target):
            continue
        if isinstance(descriptor, classmethod):
            kind = "classmethod"
        elif isinstance(descriptor, staticmethod):
            kind = "staticmethod"
        else:
            kind = "method"
        signatures[f"GPUArbiterStore.{name}"] = {
            "kind": kind,
            "signature": str(inspect.signature(target)),
        }

    for name in ledger.__all__:
        value = getattr(ledger, name)
        if inspect.isfunction(value):
            signatures[name] = {
                "kind": "function",
                "signature": str(inspect.signature(value)),
            }
    return signatures


def _contract_snapshot() -> dict[str, object]:
    enums: dict[str, object] = {}
    dataclass_shapes: dict[str, object] = {}
    for name in ledger.__all__:
        value = getattr(ledger, name)
        if inspect.isclass(value) and issubclass(value, enum.Enum):
            enums[name] = [
                (declared_name, member.name, member.value)
                for declared_name, member in value.__members__.items()
            ]
        elif inspect.isclass(value) and dataclasses.is_dataclass(value):
            dataclass_shapes[name] = {
                "params": _dataclass_params(value),
                "signature": str(inspect.signature(value)),
                "fields": [
                    {
                        "name": field.name,
                        "type": inspect.formatannotation(field.type),
                        "default": (
                            "<missing>"
                            if field.default is dataclasses.MISSING
                            else repr(field.default)
                        ),
                        "default_factory": (
                            "<missing>"
                            if field.default_factory is dataclasses.MISSING
                            else _default_factory_name(field.default_factory)
                        ),
                        "init": field.init,
                        "repr": field.repr,
                        "compare": field.compare,
                        "hash": field.hash,
                        "kw_only": field.kw_only,
                    }
                    for field in dataclasses.fields(value)
                ],
            }

    signatures = _store_signatures()

    return {
        "constants": {
            "GPUBackendMembershipState": str(ledger.GPUBackendMembershipState),
            "GPU_COLD_ADMISSION_OPERATION": ledger.GPU_COLD_ADMISSION_OPERATION,
            "GPU_EVICTION_OPERATION": ledger.GPU_EVICTION_OPERATION,
        },
        "dataclasses": dataclass_shapes,
        "enums": enums,
        "keys": dataclasses.asdict(ledger.gpu_arbiter_keys("host-a:gpu:0")),
        "signatures": signatures,
    }


def test_gpu_ledger_public_contract_matches_golden() -> None:
    snapshot = _contract_snapshot()
    mismatches = {
        name: {
            "expected": expected,
            "actual": _digest(snapshot[name]),
            "snapshot": _canonical_json(snapshot[name]),
        }
        for name, expected in _GOLDEN_SHA256.items()
        if _digest(snapshot[name]) != expected
    }
    assert not mismatches, json.dumps(mismatches, ensure_ascii=False, indent=2)
