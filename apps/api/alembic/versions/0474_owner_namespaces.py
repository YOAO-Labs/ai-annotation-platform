"""Move remaining Native owners, their local settings and bound capability logins."""

from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0474"
down_revision = "bo0473"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _one(db, sql, **params):
    return db.execute(sa.text(sql), params).scalar_one()


def _catalog(db, tokens):
    digests = defaultdict(sha256)
    query = (_SQL / "0474_owner_catalog.sql").read_text(encoding="utf-8")
    for namespace, kind, identity, definition in db.execute(
        sa.text(query), {"tokens": tokens}
    ):
        payload = json.dumps(
            [kind, identity, definition], ensure_ascii=False, separators=(",", ":")
        )
        digests[namespace].update(payload.encode("utf-8") + b"\n")
    return {name: digest.hexdigest() for name, digest in digests.items()}


def upgrade() -> None:
    db = op.get_bind()
    manifest = json.loads(
        (_SQL / "0474_owner_namespaces.json").read_text(encoding="utf-8")
    )
    required, optional = manifest["schemas"], manifest["optional_schemas"]
    names = {**required, **optional, **manifest["gucs"]}
    alternatives = "(?:" + "|".join(map(re.escape, names)) + ")"
    tokens = r"\m" + alternatives + r"\M"
    identifiers = re.compile(r"\b" + alternatives + r"\b", re.ASCII)
    quote = db.dialect.identifier_preparer.quote_identifier
    path = _one(db, "SELECT current_setting('search_path')")
    _one(db, "SELECT set_config('search_path','pg_catalog, pg_temp',true)")
    if _one(
        db,
        """SELECT EXISTS (SELECT FROM pg_prepared_xacts WHERE database=current_database())
              OR EXISTS (SELECT FROM pg_stat_activity WHERE datname=current_database()
                         AND pid<>pg_backend_pid() AND backend_type='client backend')""",
    ):
        raise RuntimeError(
            "Drain database sessions and settle prepared transactions before N5"
        )
    settings = manifest["gucs"]
    if _one(
        db,
        """SELECT EXISTS (SELECT FROM pg_db_role_setting s,
               LATERAL unnest(s.setconfig) entry WHERE s.setdatabase IN (
                   0, (SELECT oid FROM pg_database WHERE datname=current_database()))
               AND (split_part(entry,'=',1)=ANY(CAST(:settings AS text[]))
                    OR (split_part(entry,'=',1)='search_path' AND entry ~ :tokens)))""",
        settings=[*settings, *settings.values()],
        tokens=tokens,
    ):
        raise RuntimeError(
            "Remove persisted Native transaction settings or legacy search paths before N5"
        )
    present = set(
        db.execute(
            sa.text("SELECT nspname FROM pg_namespace WHERE left(nspname,4)='aap_'")
        ).scalars()
    )
    if not set(required) <= present <= required.keys() | optional.keys():
        raise RuntimeError(
            f"Unexpected N5 source schemas: {sorted(present ^ set(required))}"
        )
    destinations = [names[n] for n in required.keys() | optional.keys()]
    if _one(
        db,
        "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=ANY(CAST(:names AS text[])))",
        names=destinations,
    ):
        raise RuntimeError("N5 destination schema already exists")
    # A clean historical install has no application grants. Once provisioned, the
    # admission fence must already be visible to other transactions, not set here.
    if _one(
        db,
        """SELECT datconnlimit<>0 AND (EXISTS (
            SELECT FROM pg_namespace n, pg_roles r
            WHERE left(n.nspname,4)='aap_' AND r.rolcanlogin AND NOT r.rolsuper
              AND r.rolname<>current_user AND has_schema_privilege(r.oid,n.oid,'USAGE'))
            OR EXISTS (SELECT FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a
                WHERE left(n.nspname,4)='aap_' AND a.grantee NOT IN (n.nspowner,current_user::regrole)))
            FROM pg_database WHERE datname=current_database()""",
    ):
        raise RuntimeError("Close database admission (CONNECTION LIMIT 0) before N5")
    _validate_owners(db, tokens)
    actual = _catalog(db, tokens)
    expected = {
        n: h
        for n, h in manifest["catalog_sha256"].items()
        if n not in optional or n in present
    }
    # A PostgreSQL logical restore reparses four CHECK expressions and three
    # trigger predicates. Accept only the verified complete catalog digests.
    restored = manifest["restored_catalog_sha256"]
    differences = sorted(
        n
        for n in actual.keys() | expected.keys()
        if n not in expected
        or n not in actual
        or actual[n] not in (expected[n], restored.get(n))
    )
    if differences:
        raise RuntimeError(
            "Unknown Native definitions or dependents: " + ", ".join(differences)
        )
    # Keep original role and object OIDs: no GRANT, copying of data, replacement
    # view, second writer or privilege inherited from a newly created object.
    for old, new in manifest["logins"].items():
        bound = _one(
            db,
            """SELECT EXISTS (SELECT FROM pg_shdepend d JOIN pg_roles r
            ON d.refclassid='pg_authid'::regclass AND d.refobjid=r.oid
            JOIN pg_database b ON b.datname=current_database() WHERE r.rolname=:old
            AND (d.dbid=b.oid OR (d.classid='pg_database'::regclass AND d.objid=b.oid)))""",
            old=old,
        )
        if not bound:
            continue
        safe = _one(
            db,
            """SELECT NOT (r.rolcanlogin OR r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
            OR r.rolreplication OR r.rolbypassrls) AND NOT EXISTS (SELECT FROM pg_roles WHERE rolname=:new)
            AND NOT EXISTS (SELECT FROM pg_stat_activity WHERE usesysid=r.oid)
            AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member=r.oid OR roleid=r.oid)
            AND NOT EXISTS (SELECT FROM pg_shdepend d WHERE d.refclassid='pg_authid'::regclass
                AND d.refobjid=r.oid AND (d.deptype='o' OR (d.dbid<>b.oid AND NOT
                    (d.classid='pg_database'::regclass AND d.objid=b.oid))))
            AND NOT EXISTS (SELECT FROM pg_db_role_setting WHERE setrole=r.oid AND setdatabase NOT IN (0,b.oid))
            FROM pg_roles r, pg_database b WHERE r.rolname=:old AND b.datname=current_database()""",
            old=old,
            new=new,
        )
        if not safe:
            raise RuntimeError(
                f"Unsettled, elevated, shared or colliding capability login: {old}"
            )
        db.exec_driver_sql(f"ALTER ROLE {quote(old)} RENAME TO {quote(new)}")
    for old in sorted(present):
        db.exec_driver_sql(f"ALTER SCHEMA {quote(old)} RENAME TO {quote(names[old])}")
    routines = db.execute(
        sa.text("""SELECT p.oid, p.prosrc, pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname NOT IN ('information_schema','pg_catalog') AND p.prosrc ~ :tokens
        ORDER BY p.oid"""),
        {"tokens": tokens},
    ).all()
    for oid, body, ddl in routines:
        updated = identifiers.sub(lambda match: names[match[0]], body)
        if updated == body or ddl.count(body) != 1:
            raise RuntimeError(f"Ambiguous Native body: {oid}")
        db.exec_driver_sql(ddl.replace(body, updated, 1))
    _one(db, "SELECT set_config('search_path',:path,true)", path=path)


def _validate_owners(db, tokens):
    if _one(
        db,
        """WITH n AS (SELECT * FROM pg_namespace WHERE left(nspname,4)='aap_'),
        owned(owner,acl,kind) AS (
           SELECT nspowner,nspacl,'n'::\"char\" FROM n UNION ALL
           SELECT p.proowner,p.proacl,'f'::\"char\" FROM pg_proc p JOIN pg_namespace ns ON ns.oid=p.pronamespace
              WHERE ns.oid IN (SELECT oid FROM n) OR (ns.nspname NOT IN ('pg_catalog','information_schema')
                    AND p.prosrc ~ :tokens AND p.prosecdef) UNION ALL
           SELECT c.relowner,c.relacl,'r'::\"char\" FROM pg_class c JOIN n ON n.oid=c.relnamespace)
        SELECT EXISTS (SELECT FROM owned o JOIN pg_roles r ON r.oid=o.owner
            WHERE (r.rolname<>current_user AND (r.rolcanlogin OR r.rolsuper OR r.rolcreaterole
                OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls OR EXISTS (
                   SELECT FROM pg_auth_members WHERE roleid=r.oid AND member<>current_user::regrole)))
            OR EXISTS (SELECT FROM aclexplode(coalesce(o.acl,acldefault(o.kind,o.owner))) WHERE grantee=0))
        OR EXISTS (SELECT FROM n, LATERAL aclexplode(n.nspacl) a WHERE a.privilege_type='CREATE'
                   AND a.grantee NOT IN (n.nspowner,current_user::regrole))
        OR EXISTS (SELECT FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN n ON n.oid=c.relnamespace,
                   LATERAL aclexplode(a.attacl) acl WHERE acl.grantee=0)""",
        tokens=tokens,
    ):
        raise RuntimeError("Unexpected Native owner, schema CREATE or PUBLIC privilege")


def downgrade() -> None:
    raise NotImplementedError("Beta capability namespace cutover is forward-only")
