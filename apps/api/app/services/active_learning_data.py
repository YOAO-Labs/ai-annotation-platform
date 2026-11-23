"""Fixed, caller-transaction capabilities for selection evidence and execution."""

from types import SimpleNamespace

from sqlalchemy import Integer, String, bindparam, column, table, text
from sqlalchemy.dialects.postgresql import JSONB, UUID


def _statement(sql, *json_arguments):
    return text(sql).bindparams(
        *(bindparam(key, type_=JSONB(none_as_null=True)) for key in json_arguments)
    )


async def _execute(db, statement, **params):
    if db.autoflush:
        await db.flush()
    return await db.execute(statement, params)


async def _one(db, statement, **params):
    row = (await _execute(db, statement, **params)).mappings().one_or_none()
    return SimpleNamespace(**row) if row is not None else None


async def _many(db, statement, **params):
    return [
        SimpleNamespace(**row)
        for row in (await _execute(db, statement, **params)).mappings()
    ]


async def _scalar(db, statement, **params):
    return (await _execute(db, statement, **params)).scalar_one()


_CREATE = _statement(
    "SELECT * FROM yoao_learning_v1.selection_create_round("
    ":id, :project, :creator, :model, :deployment, :request, :request_digest, :name, "
    ":spec, :spec_digest, :authorization, :storage, :candidate_requests)",
    "spec",
    "authorization",
    "storage",
)
_ROUND = text("SELECT * FROM yoao_learning_v1.selection_round(:project, :round, :mode)")
_ROUND_REQUEST = text(
    "SELECT * FROM yoao_learning_v1.selection_round_request(:project, :request)"
)
_ROUND_JOB = text("SELECT * FROM yoao_learning_v1.selection_round_for_job(:job)")
_ROUNDS = text(
    "SELECT * FROM yoao_learning_v1.selection_rounds(:project, :task, :limit, :offset)"
)
_ROUND_COUNT = text("SELECT yoao_learning_v1.selection_round_count(:project, :task)")
_ATTEMPT = text("SELECT * FROM yoao_learning_v1.selection_attempt(:attempt, :round)")
_RECENT_ATTEMPTS = text(
    "SELECT * FROM yoao_learning_v1.selection_recent_attempts(:round)"
)
_ATTEMPT_IDS = text("SELECT * FROM yoao_learning_v1.selection_attempt_ids(:round)")
_CANDIDATES = text(
    "SELECT * FROM yoao_learning_v1.selection_candidates(:round, :ordinal)"
)
_DUE = text("SELECT * FROM yoao_learning_v1.selection_due_rounds(:cutoff)")
_OPEN = text(
    "SELECT * FROM yoao_learning_v1.selection_open_attempt(:round, :generation, :previous, :attempt, :job, :at)"
)
_CLAIM = text(
    "SELECT yoao_learning_v1.selection_claim_attempt(:attempt, :generation, :claim, :at)"
)
_TOUCH = text(
    "SELECT yoao_learning_v1.selection_touch_attempt(:attempt, :generation, :claim, :event, :at)"
)
_DEBIT = text(
    "SELECT yoao_learning_v1.selection_debit_round(:attempt, :generation, :claim, :at)"
)
_REQUEST = _statement(
    "SELECT yoao_learning_v1.selection_candidate_request(:attempt, :generation, :claim, :ordinal, :request, :media)",
    "request",
    "media",
)
_RESULT = _statement(
    "SELECT yoao_learning_v1.selection_candidate_evidence(:attempt, :generation, :claim, :ordinal, :evidence)",
    "evidence",
)
_SETTLE = _statement(
    "SELECT yoao_learning_v1.selection_settle_attempt(:attempt, :generation, :claim, :status, :code, :message, :report, :at)",
    "report",
)
_RELEASE = text(
    "SELECT yoao_learning_v1.selection_release_cancelled_claim(:attempt, :generation, :claim)"
)
_DECISION_REQUEST = text(
    "SELECT * FROM yoao_learning_v1.selection_decision_request(:round, :request)"
)
_DECISIONS = text(
    "SELECT * FROM yoao_learning_v1.selection_decisions(:round, :limit, :offset)"
)
_ACCEPTED_DECISIONS = text(
    "SELECT * FROM yoao_learning_v1.selection_accepted_decisions(:round)"
)
_RECORD_DECISION = _statement(
    "SELECT * FROM yoao_learning_v1.selection_record_decision(:round, :attempt, :actor, :request, :request_digest, "
    ":action, :batch, :ordinals, :snapshot, :snapshot_digest)",
    "snapshot",
)
_FEEDBACK_REQUEST = text(
    "SELECT * FROM yoao_learning_v1.selection_feedback_request(:round, :request)"
)
_FEEDBACK = text(
    "SELECT * FROM yoao_learning_v1.selection_feedback(:round, :limit, :offset)"
)
_RECEIPT_COUNT = text(
    "SELECT yoao_learning_v1.selection_receipt_count(:round, :feedback)"
)
_RECORD_FEEDBACK = _statement(
    "SELECT * FROM yoao_learning_v1.selection_record_feedback(:round, :actor, :request, :request_digest, :snapshot, :snapshot_digest)",
    "snapshot",
)
_RETAIN = _statement(
    "SELECT yoao_learning_v1.selection_retain_history(:round, :refs)", "refs"
)
_REFERENCE = text(
    "SELECT yoao_learning_v1.selection_history_reference(:project, :dataset, :items, :tasks, :version, :evaluation)"
)
_HISTORY_BUDGET = text(
    "SELECT * FROM yoao_learning_v1.selection_dataset_history_budget(:project)"
)
_HISTORY = text(
    "SELECT * FROM yoao_learning_v1.selection_dataset_history(:project, :share)"
)
_ADOPTION_ATTEMPTS = text(
    "SELECT * FROM yoao_learning_v1.selection_adoption_attempts(:round)"
)
_ADOPTION_CANDIDATES = text(
    "SELECT * FROM yoao_learning_v1.selection_adoption_candidates(:round)"
)
_PIN_STORAGE = _statement(
    "SELECT yoao_learning_v1.selection_pin_storage(:project, :round, :generation, :storage)",
    "storage",
)

storage_owner_row = table(
    "selection_storage_owners",
    column("id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    column("generation", Integer),
    column("current_attempt_id", UUID(as_uuid=True)),
    column("status", String),
    column("storage_ref", JSONB),
    schema="yoao_learning_v1",
)


async def create_round(db, round, candidate_requests):
    return await _one(
        db,
        _CREATE,
        id=round.id,
        project=round.project_id,
        creator=round.creator_id,
        model=round.model_version_id,
        deployment=round.deployment_id,
        request=round.request_id,
        request_digest=round.request_digest,
        name=round.name,
        spec=round.spec,
        spec_digest=round.spec_digest,
        authorization=round.authorization,
        storage=round.storage_ref,
        candidate_requests=candidate_requests,
    )


async def get_round(db, project_id, round_id, *, lock=False):
    return await _one(
        db,
        _ROUND,
        project=project_id,
        round=round_id,
        mode="update" if lock else "read",
    )


async def lock_round_for_maintenance(db, round_id):
    return await _one(db, _ROUND, project=None, round=round_id, mode="maintenance")


async def round_request(db, project_id, request_id):
    return await _one(db, _ROUND_REQUEST, project=project_id, request=request_id)


async def round_for_job(db, job_id):
    return await _one(db, _ROUND_JOB, job=job_id)


async def rounds_page(db, project_id, task, limit, offset):
    return await _many(
        db, _ROUNDS, project=project_id, task=task, limit=limit, offset=offset
    )


async def round_count(db, project_id, task):
    return await _scalar(db, _ROUND_COUNT, project=project_id, task=task)


async def get_attempt(db, attempt_id, *, round_id=None):
    return await _one(db, _ATTEMPT, attempt=attempt_id, round=round_id)


async def recent_attempts(db, round_id):
    return await _many(db, _RECENT_ATTEMPTS, round=round_id)


async def attempt_ids(db, round_id):
    return (await _execute(db, _ATTEMPT_IDS, round=round_id)).scalars().all()


async def candidates(db, round_id):
    return await _many(db, _CANDIDATES, round=round_id, ordinal=None)


async def get_candidate(db, round_id, ordinal):
    return await _one(db, _CANDIDATES, round=round_id, ordinal=ordinal)


async def due_rounds(db, cutoff):
    return (await _execute(db, _DUE, cutoff=cutoff)).scalars().all()


async def open_attempt(db, round, attempt_id, job_id, at):
    return await _one(
        db,
        _OPEN,
        round=round.id,
        generation=round.generation,
        previous=round.current_attempt_id,
        attempt=attempt_id,
        job=job_id,
        at=at,
    )


async def claim_attempt(db, attempt, claim_id, at):
    return await _scalar(
        db,
        _CLAIM,
        attempt=attempt.id,
        generation=attempt.generation,
        claim=claim_id,
        at=at,
    )


def _fence(attempt):
    return {
        "attempt": attempt.id,
        "generation": attempt.generation,
        "claim": attempt.claim_id,
    }


async def touch_attempt(db, attempt, event, at):
    return await _scalar(db, _TOUCH, **_fence(attempt), event=event, at=at)


async def debit_round(db, attempt, at):
    return await _scalar(db, _DEBIT, **_fence(attempt), at=at)


async def record_candidate_request(db, attempt, ordinal, request, media):
    return await _scalar(
        db, _REQUEST, **_fence(attempt), ordinal=ordinal, request=request, media=media
    )


async def record_candidate_evidence(db, attempt, ordinal, evidence):
    return await _scalar(
        db, _RESULT, **_fence(attempt), ordinal=ordinal, evidence=evidence
    )


async def settle_attempt(db, attempt, status, code, message, report, at):
    return await _scalar(
        db,
        _SETTLE,
        **_fence(attempt),
        status=status,
        code=code,
        message=message,
        report=report,
        at=at,
    )


async def release_cancelled_claim(db, attempt):
    return await _scalar(db, _RELEASE, **_fence(attempt))


async def decision_request(db, round_id, request_id):
    return await _one(db, _DECISION_REQUEST, round=round_id, request=request_id)


async def decisions_page(db, round_id, limit, offset):
    return await _many(db, _DECISIONS, round=round_id, limit=limit, offset=offset)


async def accepted_decisions(db, round_id):
    return await _many(db, _ACCEPTED_DECISIONS, round=round_id)


async def record_decision(
    db,
    round,
    attempt,
    actor_id,
    request_id,
    request_digest,
    action,
    batch_id,
    ordinals,
    snapshot,
    snapshot_digest,
):
    return await _one(
        db,
        _RECORD_DECISION,
        round=round.id,
        attempt=attempt.id,
        actor=actor_id,
        request=request_id,
        request_digest=request_digest,
        action=action,
        batch=batch_id,
        ordinals=ordinals,
        snapshot=snapshot,
        snapshot_digest=snapshot_digest,
    )


async def feedback_request(db, round_id, request_id):
    return await _one(db, _FEEDBACK_REQUEST, round=round_id, request=request_id)


async def feedback_page(db, round_id, limit, offset):
    return await _many(db, _FEEDBACK, round=round_id, limit=limit, offset=offset)


async def receipt_count(db, round_id, *, feedback=False):
    return await _scalar(db, _RECEIPT_COUNT, round=round_id, feedback=feedback)


async def record_feedback(
    db, round, actor_id, request_id, request_digest, snapshot, snapshot_digest
):
    return await _one(
        db,
        _RECORD_FEEDBACK,
        round=round.id,
        actor=actor_id,
        request=request_id,
        request_digest=request_digest,
        snapshot=snapshot,
        snapshot_digest=snapshot_digest,
    )


async def retain_history(db, round_id, references):
    await _scalar(db, _RETAIN, round=round_id, refs=references)


async def history_reference(
    db, project_id, dataset_id, item_ids, task_ids, version_id, evaluation_id
):
    return await _scalar(
        db,
        _REFERENCE,
        project=project_id,
        dataset=dataset_id,
        items=item_ids,
        tasks=task_ids,
        version=version_id,
        evaluation=evaluation_id,
    )


async def dataset_history_budget(db, project_id):
    return (await _execute(db, _HISTORY_BUDGET, project=project_id)).one()


async def dataset_history(db, project_id, *, lock=False):
    return await _many(db, _HISTORY, project=project_id, share=lock)


async def adoption_attempts(db, round_id):
    return await _many(db, _ADOPTION_ATTEMPTS, round=round_id)


async def adoption_candidates(db, round_id):
    return await _many(db, _ADOPTION_CANDIDATES, round=round_id)


async def pin_storage(db, round, storage_ref):
    return await _scalar(
        db,
        _PIN_STORAGE,
        project=round.project_id,
        round=round.id,
        generation=round.generation,
        storage=storage_ref,
    )
