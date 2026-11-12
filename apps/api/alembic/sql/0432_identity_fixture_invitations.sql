-- The predeclared fixture intent is consumed in Identity's real write transaction.
CREATE FUNCTION aap_identity_invitations_v1.lock_fixture_invitation_run(p_run_id uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_family text; v_parent uuid;
BEGIN
    SELECT family, parent_run_id INTO v_family, v_parent
    FROM yoao_fixture_v1.fixture_runs WHERE id=p_run_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_run_closed';
    END IF;
    IF v_family='owned' AND v_parent IS NOT NULL THEN
        PERFORM 1 FROM yoao_fixture_v1.fixture_runs
        WHERE id=v_parent AND family='owned' AND parent_run_id IS NULL AND state='open'
        FOR SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_run_closed';
        END IF;
    ELSIF v_family <> 'screenshot' OR v_parent IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_run_closed';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=p_run_id AND family=v_family AND parent_run_id IS NOT DISTINCT FROM v_parent
      AND state='open' FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_run_closed';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.lock_fixture_invitation_run(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.bind_fixture_invitation()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_run uuid;
    v_inviter_run uuid;
    v_intent yoao_fixture_v1.fixture_invitation_intents%ROWTYPE;
BEGIN
    SELECT resource.run_id INTO v_project_run
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id AND run.family IN ('screenshot','owned')
    WHERE resource.kind='project' AND resource.resource_id=NEW.project_id AND resource.state <> 'retired';
    SELECT resource.run_id INTO v_inviter_run
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id AND run.family IN ('screenshot','owned')
    WHERE resource.kind='user' AND resource.resource_id=NEW.invited_by AND resource.state <> 'retired';
    IF v_project_run IS NULL AND v_inviter_run IS NULL THEN
        RETURN NEW;
    END IF;
    IF v_project_run IS NULL OR v_project_run IS DISTINCT FROM v_inviter_run THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_scope_changed';
    END IF;
    PERFORM aap_identity_invitations_v1.lock_fixture_invitation_run(v_project_run);
    SELECT intent.* INTO v_intent
    FROM yoao_fixture_v1.fixture_invitation_intents AS intent
    JOIN yoao_fixture_v1.fixture_run_admissions AS admission
      ON admission.run_id=intent.run_id AND admission.id=intent.admission_id AND admission.state='active'
    WHERE intent.run_id=v_project_run AND intent.project_id=NEW.project_id
      AND intent.inviter_id=NEW.invited_by AND intent.email=NEW.email AND intent.state='prepared'
    FOR UPDATE OF intent;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_intent_missing';
    END IF;
    INSERT INTO yoao_fixture_v1.fixture_run_resources (run_id,kind,resource_id,logical_key,state)
    VALUES (v_intent.run_id,'invitation',NEW.id,'invitation/' || v_intent.id::text,'published');
    UPDATE yoao_fixture_v1.fixture_invitation_intents
    SET invitation_id=NEW.id,state='invited' WHERE id=v_intent.id;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.bind_fixture_invitation() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_invitation_birth_guard
BEFORE INSERT ON public.user_invitations FOR EACH ROW
EXECUTE FUNCTION aap_identity_invitations_v1.bind_fixture_invitation();
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.bind_fixture_invitation_birth(
    p_invitation_id uuid, p_user_id uuid
)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent yoao_fixture_v1.fixture_invitation_intents%ROWTYPE;
BEGIN
    SELECT * INTO v_intent FROM yoao_fixture_v1.fixture_invitation_intents
    WHERE invitation_id=p_invitation_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM aap_identity_invitations_v1.lock_fixture_invitation_run(v_intent.run_id);
    SELECT intent.* INTO v_intent
    FROM yoao_fixture_v1.fixture_invitation_intents AS intent
    JOIN yoao_fixture_v1.fixture_run_admissions AS admission
      ON admission.run_id=intent.run_id AND admission.id=intent.admission_id AND admission.state='active'
    WHERE intent.invitation_id=p_invitation_id AND intent.state='invited'
    FOR UPDATE OF intent;
    IF NOT FOUND OR NOT EXISTS (SELECT FROM public.users AS account
                                WHERE account.id=p_user_id AND account.email=v_intent.email) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_account_changed';
    END IF;
    INSERT INTO yoao_fixture_v1.fixture_run_resources (run_id,kind,resource_id,logical_key,state)
    VALUES (v_intent.run_id,'user',p_user_id,'invited/' || v_intent.id::text,'published');
    UPDATE yoao_fixture_v1.fixture_invitation_intents
    SET accepted_user_id=p_user_id,state='accepted' WHERE id=v_intent.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.bind_fixture_invitation_birth(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_invitations_v1.guard_fixture_invitation_acceptance()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_intent yoao_fixture_v1.fixture_invitation_intents%ROWTYPE;
BEGIN
    IF NEW.accepted_user_id IS NOT DISTINCT FROM OLD.accepted_user_id THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_intent FROM yoao_fixture_v1.fixture_invitation_intents
    WHERE invitation_id=NEW.id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    PERFORM aap_identity_invitations_v1.lock_fixture_invitation_run(v_intent.run_id);
    SELECT intent.* INTO v_intent
    FROM yoao_fixture_v1.fixture_invitation_intents AS intent
    JOIN yoao_fixture_v1.fixture_run_admissions AS admission
      ON admission.run_id=intent.run_id AND admission.id=intent.admission_id AND admission.state='active'
    WHERE intent.invitation_id=NEW.id AND intent.state IN ('invited','accepted')
    FOR UPDATE OF intent;
    IF NOT FOUND OR OLD.accepted_user_id IS NOT NULL OR NEW.accepted_user_id IS NULL
       OR (v_intent.state='accepted' AND v_intent.accepted_user_id IS DISTINCT FROM NEW.accepted_user_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_invitation_acceptance_changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_invitations_v1.guard_fixture_invitation_acceptance() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_invitation_acceptance_guard
BEFORE UPDATE OF accepted_user_id ON public.user_invitations FOR EACH ROW
EXECUTE FUNCTION aap_identity_invitations_v1.guard_fixture_invitation_acceptance();
