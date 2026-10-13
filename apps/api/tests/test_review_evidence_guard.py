"""Pure regression tests for the B2 review-evidence authorization guard.

No database: the guard is a pure function over a task's frozen evidence.  It
must fail closed (409) on unknown/malformed/incomplete evidence and deny (403)
a contributor, the submitter or the effective annotator.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.db.models.task import Task
from app.services.annotation_evidence import (
    assert_review_evidence_current,
    current_review_evidence,
)

_ROUND = uuid.uuid4()
_SUBMITTER = uuid.uuid4()
_CONTRIBUTOR = uuid.uuid4()
_ACCUMULATED = uuid.uuid4()
_ACTOR = uuid.uuid4()
_EFFECTIVE = uuid.uuid4()


def _task(**overrides) -> Task:
    values = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "review_round_id": _ROUND,
        "annotation_contributor_ids": [str(_ACCUMULATED)],
        "review_contributor_ids": [
            str(_ACCUMULATED),
            str(_SUBMITTER),
            str(_CONTRIBUTOR),
        ],
        "review_submitter_id": _SUBMITTER,
    }
    values.update(overrides)
    return Task(**values)


def _detail(exc: HTTPException) -> dict:
    return exc.value.detail


def test_valid_evidence_returns_frozen_set_and_submitter() -> None:
    evidence = current_review_evidence(_task())
    assert evidence is not None
    contributors, submitter = evidence
    assert submitter == str(_SUBMITTER)
    assert set(contributors) == {
        str(_ACCUMULATED),
        str(_SUBMITTER),
        str(_CONTRIBUTOR),
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"annotation_contributor_ids": None},
        {"annotation_contributor_ids": [str(_ACCUMULATED), "not-a-uuid"]},
        {"annotation_contributor_ids": "malformed"},
        {"review_round_id": None},
        {"review_contributor_ids": None},
        {"review_contributor_ids": []},
        {"review_contributor_ids": [str(_CONTRIBUTOR)]},  # missing accumulated
        {"review_contributor_ids": [str(_ACCUMULATED)]},  # missing submitter
        {"review_submitter_id": None},
        {"review_submitter_id": "not-a-uuid"},
    ],
)
def test_unknown_or_incomplete_evidence_is_409(overrides) -> None:
    with pytest.raises(HTTPException) as exc:
        assert_review_evidence_current(_task(**overrides), _ACTOR)
    assert exc.value.status_code == 409
    assert _detail(exc)["reason"] == "review_contributors_unknown"


def test_unrelated_actor_is_allowed() -> None:
    assert assert_review_evidence_current(_task(), _ACTOR) is None


def test_frozen_contributor_is_denied() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_review_evidence_current(_task(), _CONTRIBUTOR)
    assert exc.value.status_code == 403
    assert _detail(exc)["reason"] == "self_review_denied"


def test_round_submitter_is_denied() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_review_evidence_current(_task(), _SUBMITTER)
    assert exc.value.status_code == 403


def test_effective_annotator_is_denied() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_review_evidence_current(
            _task(), _EFFECTIVE, effective_annotator_id=_EFFECTIVE
        )
    assert exc.value.status_code == 403


def test_malformed_actor_id_is_denied() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_review_evidence_current(_task(), "not-a-uuid")
    assert exc.value.status_code == 403
