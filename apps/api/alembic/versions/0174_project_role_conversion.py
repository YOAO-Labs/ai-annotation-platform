"""Convert global staff roles and backfill invitation project roles.

This is the Increment-B data conversion that follows the additive preparation
of revision 0173.

What it does (deterministically, without guessing):

* ``users.role`` values ``annotator`` / ``reviewer`` become ``employee``, and
  the column's server default becomes ``employee`` so inserts that omit the
  role create usable new-model accounts.
  Account IDs, activation state, lifecycle metadata and every other column are
  preserved; inactive accounts are converted without being reactivated.
* Pending project invitations gain an explicit ``project_role`` from the role
  they already carried (an already populated ``project_role`` is preserved) and
  their platform role is normalised to ``employee`` (or kept ``viewer``).
  Accepted / revoked / expired invitations are left untouched as historical
  facts.
* Pending account-only staff invitations adopt ``employee`` with no project
  role.

What it deliberately does NOT do:

* It never creates memberships from a global role and never derives authority
  from ``users.role``.  A missing membership stays missing and fails closed.
* It does not add final ``CHECK`` constraints.  Unknown or unexpected legacy
  values must be reconciled by an operator through the audit gate first; the
  final constraints belong to the compatibility-cleanup increment.
* It does not drop the legacy ``annotator`` / ``reviewer`` code paths that
  historical records and the read-only audit still reference.

Rollback boundary
-----------------
The upgrade is not reversible.  Once new-model writes exist an employee may
legitimately be an annotator in one project and a reviewer in another, so the
old global role cannot represent the new state; deriving it from *current*
memberships would be a destructive, lossy guess.  ``downgrade()`` therefore
always refuses:

* pre-opening recovery must restore an exact protected pre-conversion snapshot;
* post-opening recovery retains the new model and fixes forward.

Revision ID: 0174
Revises: 0173
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0174"
down_revision: str | Sequence[str] | None = "0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Marker consumed by ``scripts/alembic_reversible_floor.py`` and the CI
#: round-trip check: ``downgrade()`` refuses on purpose, so automation must
#: stamp past this revision instead of executing its downgrade.
IRREVERSIBLE = True

#: Pending invitations only: accepted / revoked / expired rows are history.
_PENDING_INVITATION_PREDICATE = """
    accepted_at IS NULL
    AND revoked_at IS NULL
    AND expires_at > now()
"""


def upgrade() -> None:
    # 1. Convert the global staff identities.  Nothing else about the account
    #    changes; legacy values survive only in preserved historical records.
    op.execute(
        sa.text(
            """
            UPDATE users
            SET role = 'employee'
            WHERE role IN ('annotator', 'reviewer')
            """
        )
    )
    # 1b. Cut over the database default with the data: any insert that omits
    #     role (Core SQL, maintenance script, external tooling) must create a
    #     usable employee, not a legacy annotator the new authorization model
    #     deliberately rejects.
    op.alter_column("users", "role", server_default="employee")

    # 2. Pending project invitations: capture the project responsibility where
    #    it is not yet explicit, then normalise the platform role.  An already
    #    populated project_role is preserved verbatim.
    op.execute(
        sa.text(
            f"""
            UPDATE user_invitations
            SET project_role = role
            WHERE project_id IS NOT NULL
              AND project_role IS NULL
              AND role IN ('annotator', 'reviewer')
              AND {_PENDING_INVITATION_PREDICATE}
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE user_invitations
            SET project_role = 'viewer'
            WHERE project_id IS NOT NULL
              AND project_role IS NULL
              AND role = 'viewer'
              AND {_PENDING_INVITATION_PREDICATE}
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE user_invitations
            SET role = 'employee'
            WHERE project_id IS NOT NULL
              AND role IN ('annotator', 'reviewer')
              AND {_PENDING_INVITATION_PREDICATE}
            """
        )
    )

    # 3. Pending account-only staff invitations become employee with no project
    #    role; account-only viewer invitations keep the viewer platform role.
    op.execute(
        sa.text(
            f"""
            UPDATE user_invitations
            SET role = 'employee'
            WHERE project_id IS NULL
              AND role IN ('annotator', 'reviewer')
              AND {_PENDING_INVITATION_PREDICATE}
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "0174 is not reversible: the employee cutover cannot be losslessly "
        "mapped back to a single global role. Restore the protected "
        "pre-conversion snapshot for pre-opening recovery, or recover "
        "post-opening with a new-model binary (fix forward). A destructive "
        "downgrade is deliberately unsupported."
    )
