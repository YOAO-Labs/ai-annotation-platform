"""Unit tests for the A2 annotation-phase contributor evidence helpers.

These exercise the pure / sticky-unknown / canonicalization semantics without a
database.  The database-backed producer, lock and concurrency coverage lives in
``test_annotation_contributor_recording.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.task import Task
from app.services import annotation_evidence as evidence


class _StubSession:
    """Minimal AsyncSession stand-in recording flush/refresh order under lock."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def flush(self) -> None:
        self.calls.append(("flush",))

    async def refresh(self, instance, attribute_names=None, with_for_update=None):
        self.calls.append(("refresh", tuple(attribute_names or ()), with_for_update))


def _task(**kwargs) -> Task:
    task = Task()
    for key, value in kwargs.items():
        setattr(task, key, value)
    return task


# ── canonicalization ─────────────────────────────────────────────────


def test_canonical_actor_id_accepts_uuid_and_lowercases_strings():
    value = uuid.uuid4()
    assert evidence.canonical_actor_id(value) == str(value)
    assert evidence.canonical_actor_id(str(value).upper()) == str(value)
    assert evidence.canonical_actor_id(str(value)) == str(value)


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", 123, [], {}, True])
def test_canonical_actor_id_rejects_malformed(value):
    assert evidence.canonical_actor_id(value) is None


def test_known_ids_canonicalizes_strings():
    first, second = uuid.uuid4(), uuid.uuid4()
    task = _task(annotation_contributor_ids=[str(first).upper(), str(second)])
    assert evidence.known_annotation_contributor_ids(task) == {
        str(first),
        str(second),
    }


def test_known_ids_empty_list_is_known_empty():
    assert (
        evidence.known_annotation_contributor_ids(_task(annotation_contributor_ids=[]))
        == set()
    )


@pytest.mark.parametrize(
    "values",
    [
        None,
        "not-a-list",
        {"actor": "x"},
        [None],
        [""],
        ["a", "b"],
        [str(uuid.uuid4()), "broken"],
    ],
)
def test_known_ids_malformed_is_unknown(values):
    assert (
        evidence.known_annotation_contributor_ids(
            _task(annotation_contributor_ids=values)
        )
        is None
    )


# ── accumulation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_accumulates_canonical_actor_under_lock():
    actor = uuid.uuid4()
    task = _task(annotation_contributor_ids=[], status="in_progress")
    db = _StubSession()

    changed = await evidence.record_annotation_actor(db, task, actor)

    assert changed is True
    assert task.annotation_contributor_ids == [str(actor)]
    # Pending changes must be flushed before the locking refresh re-reads them.
    assert db.calls == [
        ("flush",),
        ("refresh", ("annotation_contributor_ids", "status"), True),
    ]


@pytest.mark.asyncio
async def test_record_canonicalizes_uppercase_actor():
    actor = uuid.uuid4()
    task = _task(annotation_contributor_ids=[], status="in_progress")

    await evidence.record_annotation_actor(
        _StubSession(), task, str(actor).upper(), lock=False
    )

    assert task.annotation_contributor_ids == [str(actor)]


@pytest.mark.asyncio
async def test_record_deduplicates_existing_actor():
    actor = uuid.uuid4()
    task = _task(annotation_contributor_ids=[str(actor)], status="in_progress")

    changed = await evidence.record_annotation_actor(
        _StubSession(), task, actor, lock=False
    )

    assert changed is False
    assert task.annotation_contributor_ids == [str(actor)]


@pytest.mark.asyncio
async def test_record_unions_and_sorts_multiple_actors():
    first, second = uuid.uuid4(), uuid.uuid4()
    task = _task(annotation_contributor_ids=[str(second)], status="in_progress")
    db = _StubSession()

    await evidence.record_annotation_actor(db, task, first)

    assert task.annotation_contributor_ids == sorted([str(first), str(second)])


@pytest.mark.asyncio
async def test_legacy_null_accumulator_stays_unknown():
    task = _task(annotation_contributor_ids=None, status="in_progress")

    changed = await evidence.record_annotation_actor(
        _StubSession(), task, uuid.uuid4(), lock=False
    )

    assert changed is False
    assert task.annotation_contributor_ids is None


@pytest.mark.asyncio
async def test_malformed_accumulator_stays_unknown():
    task = _task(annotation_contributor_ids=["broken"], status="in_progress")

    changed = await evidence.record_annotation_actor(
        _StubSession(), task, uuid.uuid4(), lock=False
    )

    assert changed is False
    assert task.annotation_contributor_ids == ["broken"]


@pytest.mark.asyncio
async def test_missing_or_malformed_actor_is_noop():
    task = _task(annotation_contributor_ids=[], status="in_progress")

    assert (
        await evidence.record_annotation_actor(_StubSession(), task, None, lock=False)
        is False
    )
    assert (
        await evidence.record_annotation_actor(
            _StubSession(), task, "broken", lock=False
        )
        is False
    )
    assert task.annotation_contributor_ids == []


@pytest.mark.asyncio
async def test_review_phase_adjustment_does_not_add_reviewer():
    task = _task(annotation_contributor_ids=[], status="review")

    changed = await evidence.record_annotation_actor(
        _StubSession(), task, uuid.uuid4(), lock=False
    )

    assert changed is False
    assert task.annotation_contributor_ids == []


# ── freeze ───────────────────────────────────────────────────────────


def test_freeze_known_accumulator_includes_submitter_and_assignee():
    actor, assignee, prior = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task = _task(annotation_contributor_ids=[str(prior)])

    frozen = evidence.freeze_review_contributor_evidence(
        task,
        submitter_id=actor,
        contributor_ids=[str(assignee), str(prior)],
    )

    assert frozen == sorted([str(actor), str(assignee), str(prior)])
    assert task.review_contributor_ids == frozen
    assert task.review_submitter_id == str(actor)


def test_freeze_unknown_accumulator_does_not_manufacture_known_set():
    actor = uuid.uuid4()
    task = _task(annotation_contributor_ids=None)

    frozen = evidence.freeze_review_contributor_evidence(
        task, submitter_id=actor, contributor_ids=[str(actor)]
    )

    assert frozen is None
    assert task.review_contributor_ids is None
    assert task.review_submitter_id == str(actor)


def test_freeze_malformed_contributor_makes_round_unknown():
    task = _task(annotation_contributor_ids=[])

    frozen = evidence.freeze_review_contributor_evidence(
        task, submitter_id=uuid.uuid4(), contributor_ids=["not-a-uuid"]
    )

    assert frozen is None
    assert task.review_contributor_ids is None


def test_freeze_invalid_submitter_makes_round_unknown():
    task = _task(annotation_contributor_ids=[])

    frozen = evidence.freeze_review_contributor_evidence(
        task, submitter_id="broken", contributor_ids=[]
    )

    assert frozen is None
    assert task.review_contributor_ids is None
    assert task.review_submitter_id is None


def test_freeze_empty_accumulator_keeps_submitter_only():
    actor = uuid.uuid4()
    task = _task(annotation_contributor_ids=[])

    frozen = evidence.freeze_review_contributor_evidence(
        task, submitter_id=actor, contributor_ids=[]
    )

    assert frozen == [str(actor)]


def test_clear_invalidates_round_but_retains_accumulator():
    task = _task(
        annotation_contributor_ids=["keep-me"],
        review_contributor_ids=["round"],
        review_submitter_id=uuid.uuid4(),
    )

    evidence.clear_review_contributor_evidence(task)

    assert task.review_contributor_ids is None
    assert task.review_submitter_id is None
    assert task.annotation_contributor_ids == ["keep-me"]
