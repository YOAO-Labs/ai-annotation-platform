-- Attribution only: preserve the existing first visible profile selection.
CREATE FUNCTION aap_identity_v1.read_first_seed_account()
RETURNS TABLE(id uuid, email text)
LANGUAGE sql STABLE PARALLEL RESTRICTED SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT account.id, account.email
    FROM public.users AS account
    JOIN public.identity_account_presence AS presence ON presence.user_id = account.id
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.read_first_seed_account() FROM PUBLIC;
