-- Lock Identity-owned account rows inside the caller's current transaction.
-- Invalid arguments raise SQLSTATE 22023 (invalid_request). Lock failures
-- 55P03, 40001, and 40P01 propagate unchanged and abort the caller transaction.
CREATE FUNCTION aap_identity_v1.lock_accounts_share(
    p_account_ids uuid[],
    p_nowait boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_accounts jsonb;
BEGIN
    IF p_account_ids IS NULL OR p_nowait IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.cardinality(p_account_ids) > 0
       AND pg_catalog.array_ndims(p_account_ids) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.array_position(p_account_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF p_nowait THEN
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR SHARE OF account NOWAIT
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    ELSE
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR SHARE OF account
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-account-locks/v1',
        'accounts', v_accounts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_accounts_share(uuid[], boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.lock_accounts_update(
    p_account_ids uuid[],
    p_nowait boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_accounts jsonb;
BEGIN
    IF p_account_ids IS NULL OR p_nowait IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.cardinality(p_account_ids) > 0
       AND pg_catalog.array_ndims(p_account_ids) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.array_position(p_account_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF p_nowait THEN
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR UPDATE OF account NOWAIT
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    ELSE
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR UPDATE OF account
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-account-locks/v1',
        'accounts', v_accounts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_accounts_update(uuid[], boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.lock_accounts_key_share(
    p_account_ids uuid[],
    p_nowait boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_accounts jsonb;
BEGIN
    IF p_account_ids IS NULL OR p_nowait IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.cardinality(p_account_ids) > 0
       AND pg_catalog.array_ndims(p_account_ids) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    IF pg_catalog.array_position(p_account_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF p_nowait THEN
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR KEY SHARE OF account NOWAIT
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    ELSE
        WITH locked AS MATERIALIZED (
            SELECT account.id, account.name, account.email,
                   account.role, account.is_active
            FROM public.users AS account
            WHERE account.id = ANY (p_account_ids)
            ORDER BY account.id
            FOR KEY SHARE OF account
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', locked.id,
                    'name', locked.name,
                    'email', locked.email,
                    'role', locked.role,
                    'is_active', locked.is_active
                ) ORDER BY locked.id
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM locked;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-account-locks/v1',
        'accounts', v_accounts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_accounts_key_share(uuid[], boolean)
    FROM PUBLIC;
