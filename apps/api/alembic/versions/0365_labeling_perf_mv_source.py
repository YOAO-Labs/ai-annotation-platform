"""Read user-performance aggregates through Labeling's public task-event facts."""

from sqlalchemy import text
from alembic import op

revision = "0365"
down_revision = "0364"
branch_labels = depends_on = None

_OWNER = text(
    """
    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid = pg_catalog.to_regclass('public.mv_user_perf_daily')
      AND relation.relkind = 'm'
    """
)

_EXPLICIT_GRANTS = text(
    """
    SELECT pg_catalog.format(
        'GRANT %s ON TABLE public.mv_user_perf_daily TO %s%s',
        privilege.privilege_type,
        CASE WHEN privilege.grantee = 0 THEN 'PUBLIC'
             ELSE pg_catalog.format('%I', grantee.rolname) END,
        CASE WHEN privilege.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
    )
    FROM pg_catalog.pg_class AS relation
    CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS privilege
    LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
    WHERE relation.oid = pg_catalog.to_regclass('public.mv_user_perf_daily')
      AND privilege.grantee <> relation.relowner
    ORDER BY privilege.grantor, privilege.grantee,
             privilege.privilege_type, privilege.is_grantable
    """
)

_NEW_GRANT_REVOKES = text(
    """
    SELECT pg_catalog.format(
        'REVOKE ALL PRIVILEGES ON TABLE public.mv_user_perf_daily FROM %s CASCADE',
        CASE WHEN privilege.grantee = 0 THEN 'PUBLIC'
             ELSE pg_catalog.format('%I', grantee.rolname) END
    )
    FROM pg_catalog.pg_class AS relation
    CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS privilege
    LEFT JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
    WHERE relation.oid = pg_catalog.to_regclass('public.mv_user_perf_daily')
      AND privilege.grantee <> relation.relowner
    GROUP BY privilege.grantee, grantee.rolname
    ORDER BY privilege.grantee
    """
)


def _replace_user_perf_mv(source: str) -> None:
    bind = op.get_bind()
    owner = bind.execute(_OWNER).scalar_one()
    grants = bind.execute(_EXPLICIT_GRANTS).scalars().all()

    op.execute("DROP MATERIALIZED VIEW public.mv_user_perf_daily")
    op.execute(
        f"""
        CREATE MATERIALIZED VIEW public.mv_user_perf_daily AS
        SELECT
            user_id,
            project_id,
            kind,
            (date_trunc('day', started_at AT TIME ZONE 'UTC'))::date AS day,
            COUNT(*) AS event_count,
            SUM(annotation_count) AS throughput,
            (percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::int AS median_duration_ms,
            (percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_duration_ms,
            SUM(CASE WHEN was_rejected THEN 1 ELSE 0 END) AS rejected_n,
            (SUM(duration_ms)::bigint / 60000)::int AS active_minutes
        FROM {source}
        GROUP BY user_id, project_id, kind, day
        WITH NO DATA
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_mv_user_perf_daily_pk ON public.mv_user_perf_daily "
        "(user_id, project_id, kind, day)"
    )
    op.execute(
        "CREATE INDEX ix_mv_user_perf_daily_user_day ON public.mv_user_perf_daily "
        "(user_id, day DESC)"
    )
    op.execute(
        "CREATE INDEX ix_mv_user_perf_daily_project_day ON public.mv_user_perf_daily "
        "(project_id, day DESC)"
    )
    # Do not carry migration-role default ACLs into the replacement object.
    for revoke in bind.execute(_NEW_GRANT_REVOKES).scalars().all():
        bind.exec_driver_sql(revoke)
    # Initial refresh must run as the migration authority, before restoring the
    # ordinary runtime owner whose private TaskEvent privileges are restricted.
    op.execute("REFRESH MATERIALIZED VIEW public.mv_user_perf_daily")
    owner_ddl = bind.execute(
        text(
            "SELECT pg_catalog.format("
            "'ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO %I', :owner)"
        ),
        {"owner": owner},
    ).scalar_one()
    bind.exec_driver_sql(owner_ddl)
    for grant in grants:
        bind.exec_driver_sql(grant)


def upgrade() -> None:
    _replace_user_perf_mv("aap_labeling_v1.task_event_facts")


def downgrade() -> None:
    _replace_user_perf_mv("public.task_events")
