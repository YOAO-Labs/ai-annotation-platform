from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from aap_inference_contract.execution import (
    ExecutionProvenance,
)


@dataclass(frozen=True, slots=True)
class SecondaryInferenceResult:
    write_target: Literal["attributes", "geometry"]
    model_ref: tuple[tuple[str, str | None], ...]
    attributes: tuple[tuple[str, Any], ...]
    geometry_shapes: tuple[Any, ...]
    execution_provenance: ExecutionProvenance | None = None
