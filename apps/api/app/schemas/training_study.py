"""Explicit manager budgets and a reviewed finite grid."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.schemas.training_run import TrainingRunCreate


class StudyBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    max_trials: int = Field(ge=1, le=20)
    max_planned_epochs: int = Field(ge=1, le=20000)
    max_native_seconds: float = Field(gt=0, le=604800)


class TrainingStudyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base: TrainingRunCreate
    grid: dict[str, list[JsonValue]] = Field(min_length=1, max_length=10)
    objective: Literal[
        "metrics/mAP50-95",
        "metrics/mAP50",
        "metrics/mAP75",
        "metrics/precision",
        "metrics/recall",
        "metrics/AR100",
    ]
    direction: Literal["maximize"]
    budgets: StudyBudgets
    preview_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("grid")
    @classmethod
    def bounded_values(cls, value):
        if any(not 1 <= len(values) <= 20 for values in value.values()):
            raise ValueError("每个网格参数需要 1–20 个显式值")
        return value

    @model_validator(mode="after")
    def full_configuration(self):
        if self.base.configuration is None:
            raise ValueError("参数搜索必须使用完整配置")
        if self.base.framework == "libreyolo" and self.objective in {
            "metrics/precision",
            "metrics/recall",
        }:
            raise ValueError("LibreYOLO 最终评估不提供 precision/recall")
        if self.base.framework == "ultralytics" and self.objective in {
            "metrics/mAP75",
            "metrics/AR100",
        }:
            raise ValueError("Ultralytics 最终评估不提供 mAP75/AR100")
        return self

    def request_payload(self):
        return {
            "base": self.base.request_payload(),
            "grid": self.grid,
            "objective": self.objective,
            "direction": self.direction,
            "budgets": self.budgets.model_dump(mode="json"),
        }
