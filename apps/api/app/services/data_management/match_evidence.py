"""Branch-aware evidence collection for the task match explanation drawer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, and_, literal, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services.data_management.filter_tree import validate_filter_tree
from app.services.data_management.task_filters import (
    _compile_annotation_object_condition,
    _is_annotation_object_rule,
    compile_filter,
)
from app.services.labeling_core_read_relations import task_row


_PENDING_SHAPE_FIELD = "ai.pending_prediction_shape_count"
_LOW_CONFIDENCE_SHAPE_FIELD = "ai.low_confidence_prediction_shape_count"
_TRACKER_FIELD = "ai.pending_tracker_job_count"
_CANDIDATE_FIELDS = {
    _PENDING_SHAPE_FIELD,
    _LOW_CONFIDENCE_SHAPE_FIELD,
    _TRACKER_FIELD,
}


@dataclass
class MatchEvidence:
    """SQL source scopes selected by the true filter branches."""

    project: Project
    annotation_condition: ColumnElement[bool] | None = None
    prediction_scope: str | None = None
    tracker_jobs: bool = False


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    rules = node.get("rules")
    return rules if isinstance(rules, list) else []


def _or_children(
    node: dict[str, Any], path: str = "0"
) -> list[tuple[str, dict[str, Any]]]:
    """Return only OR children, in AST order, for the bounded truth query."""

    entries: list[tuple[str, dict[str, Any]]] = []
    children = _children(node)
    if node.get("op", "and") == "or":
        entries.extend(
            (f"{path}.{index}", child) for index, child in enumerate(children)
        )
    for index, child in enumerate(children):
        entries.extend(_or_children(child, f"{path}.{index}"))
    return entries


def _is_candidate_rule(node: dict[str, Any]) -> bool:
    return node.get("field") in _CANDIDATE_FIELDS


def _annotation_condition(
    node: dict[str, Any], annotation, project: Project
) -> ColumnElement[bool]:
    return _compile_annotation_object_condition(
        annotation,
        str(node["field"]),
        str(node["op"]),
        node.get("value"),
        project,
    )


def _merge_annotation_conditions(
    conditions: list[ColumnElement[bool]],
) -> ColumnElement[bool] | None:
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return or_(*conditions)


def _true_entity_source(
    node: dict[str, Any], path: str, truths: dict[str, bool]
) -> bool:
    """Whether a true subtree has an entity source, using actual OR truth."""

    if "rules" not in node:
        return _is_annotation_object_rule(node) or _is_candidate_rule(node)

    children = _children(node)
    if node.get("op", "and") == "or":
        return any(
            truths.get(f"{path}.{index}", True)
            and _true_entity_source(child, f"{path}.{index}", truths)
            for index, child in enumerate(children)
        )

    # Immediate annotation leaves are one same-row witness when their AND group is
    # true.  Nested groups retain their own witness identity and may be different
    # rows, so recurse into each group independently.
    if any(_is_annotation_object_rule(child) for child in children):
        return True
    return any(
        _true_entity_source(child, f"{path}.{index}", truths)
        for index, child in enumerate(children)
        if "rules" in child or _is_candidate_rule(child)
    )


def _collect_true_sources(
    node: dict[str, Any],
    path: str,
    *,
    truths: dict[str, bool],
    evidence: MatchEvidence,
    suppress_context: bool = False,
) -> tuple[ColumnElement[bool] | None, bool, bool]:
    """Return annotation predicate, task-context flag, and entity-source flag."""

    if "rules" not in node:
        if _is_annotation_object_rule(node):
            return (
                _annotation_condition(node, Annotation, evidence.project),
                False,
                True,
            )
        field = node.get("field")
        if field == _PENDING_SHAPE_FIELD:
            evidence.prediction_scope = "pending"
            return None, False, True
        if field == _LOW_CONFIDENCE_SHAPE_FIELD:
            if evidence.prediction_scope != "pending":
                evidence.prediction_scope = "low"
            return None, False, True
        if field == _TRACKER_FIELD:
            evidence.tracker_jobs = True
            return None, False, True
        return None, not suppress_context, False

    children = _children(node)
    if node.get("op", "and") == "or":
        predicates: list[ColumnElement[bool]] = []
        context = False
        entity = False
        for index, child in enumerate(children):
            child_path = f"{path}.{index}"
            if not truths.get(child_path, True):
                continue
            predicate, child_context, child_entity = _collect_true_sources(
                child,
                child_path,
                truths=truths,
                evidence=evidence,
                suppress_context=suppress_context,
            )
            if predicate is not None:
                predicates.append(predicate)
            context = context or child_context
            entity = entity or child_entity
        # The task compiler compiles an empty group to literal true for either op,
        # so an empty OR explains its match with task context exactly like an empty
        # AND does, instead of serializing an empty witness set.
        if not children and not suppress_context:
            context = True
        return _merge_annotation_conditions(predicates), context, entity

    # Resolve this from the actual true branches before visiting children.  A false
    # annotation sibling in a nested OR must not suppress a true task-only branch;
    # an actual entity witness elsewhere in the AND does suppress that context.
    entity = _true_entity_source(node, path, truths)
    child_suppress_context = suppress_context or entity
    predicates: list[ColumnElement[bool]] = []
    immediate = [child for child in children if _is_annotation_object_rule(child)]
    if immediate:
        predicates.append(
            and_(
                *[
                    _annotation_condition(child, Annotation, evidence.project)
                    for child in immediate
                ]
            )
        )
    context = False
    for index, child in enumerate(children):
        if _is_annotation_object_rule(child):
            continue
        child_path = f"{path}.{index}"
        predicate, child_context, _ = _collect_true_sources(
            child,
            child_path,
            truths=truths,
            evidence=evidence,
            suppress_context=child_suppress_context,
        )
        if predicate is not None:
            predicates.append(predicate)
        context = context or child_context
    if not children and not suppress_context:
        context = True
    return _merge_annotation_conditions(predicates), context, entity


async def collect_match_evidence(
    db: AsyncSession,
    *,
    task_id: UUID,
    filter_json: dict[str, Any],
    project: Project,
    user: User | IdentityActorFacts,
) -> MatchEvidence:
    """Evaluate OR branches once, then return direct SQL witness predicates."""

    validate_filter_tree(filter_json)
    evidence = MatchEvidence(project=project)
    if not filter_json:
        evidence.annotation_condition = literal(True)
        return evidence

    # A boolean ARRAY is one PostgreSQL target-list column even for a structurally
    # valid tree containing more than 1664 OR children.  Non-OR descendants are
    # known true once their containing AND branch is reached after the full task
    # filter guard in DataManagerService.matches.
    or_entries = _or_children(filter_json)
    truths: dict[str, bool] = {}
    if or_entries:
        branch_exprs = [
            compile_filter(child, project=project, user=user) for _, child in or_entries
        ]
        truth_array = array(branch_exprs, type_=Boolean)
        row = (
            await db.execute(
                select(truth_array.label("dm_match_or_truth"))
                .select_from(task_row)
                .where(task_row.id == task_id)
            )
        ).first()
        if row is not None:
            values = row[0] or []
            truths = {
                path: bool(values[index])
                for index, (path, _) in enumerate(or_entries)
                if index < len(values)
            }

    predicate, context, _ = _collect_true_sources(
        filter_json,
        "0",
        truths=truths,
        evidence=evidence,
    )
    evidence.annotation_condition = literal(True) if context else predicate
    return evidence


__all__ = ["MatchEvidence", "collect_match_evidence"]
