"""Premise control for the global "last unreserved super-admin" rules.

``aap_identity_v1.super_admin_status`` counts every active, unreserved
``super_admin`` in ``public.users``; notifications for a self-deactivation
request fan out to every active ``super_admin`` the same way.  Those rules are
global by product definition, while the ordinary verifier profile runs the
whole API suite against one disposable database that earlier files and the
verifier's own fixture-authority worlds keep populated.  A test that asserts a
global premise therefore has to establish it for itself instead of assuming an
otherwise empty table.

The helpers park every foreign super admin (``is_active = false``) for the
duration of the test and never delete, recreate or re-key a row:

- ``park_foreign_super_admins`` serves tests that use their own committed
  sessions/connections (native module fixtures); pair it with
  ``restore_parked_super_admins`` in the test's ``finally``.
- ``park_foreign_super_admins_in_savepoint`` serves the conftest
  ``db_session`` savepoint; the fixture teardown already rolls the parking
  back, so no explicit restore is needed.

Only active foreign rows are parked because inactive super admins already fail
both global predicates.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _keep_list(keep_ids) -> list[str]:
    return [str(user_id) for user_id in keep_ids]


async def park_foreign_super_admins(test_engine, *, keep_ids) -> list[uuid.UUID]:
    """Park foreign active super admins; return the parked row identities."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        parked = list(
            await session.scalars(
                text(
                    "SELECT id FROM public.users WHERE role='super_admin' "
                    "AND is_active IS TRUE "
                    "AND id <> ALL(CAST(:keep AS uuid[]))"
                ),
                {"keep": _keep_list(keep_ids)},
            )
        )
    if parked:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET is_active=false "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": parked},
            )
    return parked


async def restore_parked_super_admins(test_engine, parked) -> None:
    """Reactivate exactly the rows parked by ``park_foreign_super_admins``."""
    ids = list(parked)
    if not ids:
        return
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        result = await session.execute(
            text(
                "UPDATE public.users SET is_active=true "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND is_active IS FALSE "
                "RETURNING id"
            ),
            {"ids": ids},
        )
        restored = set(result.scalars())
    if restored != set(ids):
        raise RuntimeError("identity super-admin premise did not restore parked rows")


async def park_foreign_super_admins_in_savepoint(
    db_session, *, keep_ids
) -> list[uuid.UUID]:
    """Park foreign active super admins inside the caller's test savepoint.

    The conftest ``db_session`` fixture rolls the enclosing transaction back at
    teardown, so the parked rows reappear unchanged for later tests.
    """
    result = await db_session.execute(
        text(
            "UPDATE public.users SET is_active=false WHERE role='super_admin' "
            "AND is_active IS TRUE "
            "AND id <> ALL(CAST(:keep AS uuid[])) "
            "RETURNING id"
        ),
        {"keep": _keep_list(keep_ids)},
    )
    return list(result.scalars())
