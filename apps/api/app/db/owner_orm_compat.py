"""SQLAlchemy hooks needed when a PostgreSQL owner executes the physical SQL."""

from inspect import signature
from types import SimpleNamespace

from sqlalchemy import __version__, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.bulk_persistence import (
    BulkORMDelete,
    BulkORMUpdate,
    BulkUDCompileState,
)
from sqlalchemy.orm.context import ORMCompileState
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BinaryExpression

_SQLALCHEMY_VERSION = "2.0.49"
_POST_ARGS = ("session", "statement", "result", "update_options")
_OPTIONAL_MODIFIERS = (
    "_for_update_arg",
    "_limit_clause",
    "_offset_clause",
    "_fetch_clause",
)
_SELECT_MODIFIERS = (
    "_with_options",
    "_order_by_clauses",
    "_group_by_clauses",
    "_having_criteria",
    "_distinct",
    "_distinct_on",
    "_prefixes",
    "_suffixes",
    "_hints",
    "_statement_hints",
    "_independent_ctes",
    "_correlate",
    "_correlate_except",
)


def _assert_installed_contract():
    hooks = (
        (Session, "_remove_newly_deleted", ("self", "states")),
        (
            BulkUDCompileState,
            "_eval_condition_from_statement",
            ("update_options", "statement"),
        ),
        (BulkORMUpdate, "_do_post_synchronize_evaluate", _POST_ARGS),
        (BulkORMUpdate, "_do_post_synchronize_fetch", _POST_ARGS),
        (BulkORMDelete, "_do_post_synchronize_evaluate", _POST_ARGS),
    )
    update_options = BulkUDCompileState.default_update_options
    refresh_options = ORMCompileState.default_compile_options
    probe = select(1)
    if (
        __version__ != _SQLALCHEMY_VERSION
        or any(
            not callable(hook := getattr(owner, name, None))
            or tuple(signature(hook).parameters) != args
            for owner, name, args in hooks
        )
        or not all(
            hasattr(update_options, name)
            for name in (
                "_subject_mapper",
                "_identity_token",
                "_matched_rows",
                "_eval_condition",
            )
        )
        or not all(
            hasattr(refresh_options, name)
            for name in ("_only_load_props", "_for_refresh_state")
        )
        or not all(
            hasattr(probe, name)
            for name in (*_SELECT_MODIFIERS, *_OPTIONAL_MODIFIERS, "_compile_options")
        )
    ):
        raise RuntimeError(
            f"Unsupported SQLAlchemy owner ORM contract ({__version__}); "
            "qualify the Jobs refresh/fetch and Notifications evaluate/rollback gates"
        )


_assert_installed_contract()


def plain_refresh_primary_key(statement, mapper, parameters):
    """Accept only the ORM's unmodified, unadorned single-PK refresh."""
    options = statement._compile_options
    if (
        not isinstance(options, ORMCompileState.default_compile_options)
        or not options._for_refresh_state
        or any(getattr(statement, name) for name in _SELECT_MODIFIERS)
        or any(getattr(statement, name) is not None for name in _OPTIONAL_MODIFIERS)
    ):
        return None
    table = mapper.local_table
    keys = mapper.primary_key
    where = statement.whereclause
    columns = statement.column_descriptions
    if (
        len(columns) != 1
        or columns[0]["expr"] is not mapper.class_
        or statement.get_final_froms() != [table]
        or len(keys) != 1
        or not isinstance(where, BinaryExpression)
        or where.operator is not operators.eq
        or not where.left.shares_lineage(keys[0])
        or not isinstance(where.right, BindParameter)
        or where.right.key not in parameters
    ):
        return None
    return where.right


def copy_refresh_options(original, rewritten):
    """Transfer the original partial projection without reissuing a refresh."""
    label_style = original.get_label_style()
    if label_style != rewritten.get_label_style():
        rewritten = rewritten.set_label_style(label_style)
    rewritten._compile_options = original._compile_options
    execution_options = original.get_execution_options()
    return (
        rewritten.execution_options(**execution_options)
        if execution_options
        else rewritten
    )


def mark_missing_identity(session, state):
    """Register a missing or owner-deleted row for Session rollback."""
    session._remove_newly_deleted([state])


def synchronize_owner_deleted_ids(session, model, affected_ids):
    """Remove exactly the loaded identities that the owner physically deleted."""
    mapper = inspect(model)
    for row_id in affected_ids:
        row = session.identity_map.get(mapper.identity_key_from_primary_key((row_id,)))
        if row is not None:
            mark_missing_identity(session, inspect(row))


def _options(model):
    return BulkUDCompileState.default_update_options + {
        "_subject_mapper": inspect(model),
        "_identity_token": None,
    }


def _update_result(statement):
    compiled = statement.compile()
    return SimpleNamespace(
        returned_defaults_rows=None,
        context=SimpleNamespace(compiled_parameters=[compiled.construct_params()]),
        prefetch_cols=lambda: compiled.prefetch,
        postfetch_cols=lambda: compiled.postfetch,
    )


def synchronize_owner_fetch(session, model, statement, affected_ids):
    """Mirror ORM UPDATE fetch synchronization using the owner's exact affected IDs."""
    matched = [(row_id, None) for row_id in affected_ids]
    if not matched:
        return
    options = _options(model) + {"_matched_rows": matched}
    BulkORMUpdate._do_post_synchronize_fetch(
        session, statement, _update_result(statement), options
    )


def synchronize_owner_evaluate(session, model, statement, *, deleting=False):
    """Mirror ORM evaluate against local values, independently of physical row count."""
    options = _options(model)
    options = options + {
        "_eval_condition": BulkUDCompileState._eval_condition_from_statement(
            options, statement
        )
    }
    if deleting:
        BulkORMDelete._do_post_synchronize_evaluate(session, statement, None, options)
    else:
        BulkORMUpdate._do_post_synchronize_evaluate(
            session, statement, _update_result(statement), options
        )
