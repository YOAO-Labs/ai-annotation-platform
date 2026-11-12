CREATE FUNCTION yoao_fixture_v1.guard_original_target()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_original_target_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_original_target() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_original_target_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_original_targets
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_original_target();
-- aap-statement-break

CREATE FUNCTION yoao_fixture_v1.validate_run_original_target()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.original_target_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_original_targets AS target
        WHERE target.id = NEW.original_target_id
          AND target.storage_identity = NEW.original_storage_identity
          AND target.store_receipt_sha256 = NEW.original_store_receipt_sha256
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_original_target_mismatch';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.validate_run_original_target() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_run_original_target_validate
BEFORE INSERT ON yoao_fixture_v1.fixture_runs
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.validate_run_original_target();
-- aap-statement-break

-- A reserved dataset's physical namespace must not drift after a reader or
-- writer has classified it; retirement retains the reservation as evidence.
CREATE FUNCTION yoao_fixture_v1.guard_dataset_storage_identity()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.resource_id IS DISTINCT FROM OLD.resource_id
       OR NEW.logical_key IS DISTINCT FROM OLD.logical_key
       OR NEW.creation_id IS DISTINCT FROM OLD.creation_id
       OR NEW.admission_id IS DISTINCT FROM OLD.admission_id
       OR NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
       OR NEW.item_id IS DISTINCT FROM OLD.item_id
       OR NEW.item_fingerprint IS DISTINCT FROM OLD.item_fingerprint THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_resource_identity_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_dataset_storage_identity() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_dataset_storage_identity_guard
BEFORE UPDATE OF run_id, kind, resource_id, logical_key, creation_id, admission_id,
    storage_identity, item_id, item_fingerprint ON yoao_fixture_v1.fixture_run_resources
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_dataset_storage_identity();
-- aap-statement-break
CREATE FUNCTION yoao_fixture_v1.guard_run_original_store()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id
       OR NEW.family IS DISTINCT FROM OLD.family
       OR NEW.scope_key IS DISTINCT FROM OLD.scope_key
       OR NEW.original_target_id IS DISTINCT FROM OLD.original_target_id
       OR NEW.original_storage_identity IS DISTINCT FROM OLD.original_storage_identity
       OR NEW.original_store_receipt_sha256 IS DISTINCT FROM OLD.original_store_receipt_sha256 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_original_store_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_run_original_store() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_run_original_store_guard
BEFORE UPDATE OF id, parent_run_id, family, scope_key, original_target_id, original_storage_identity, original_store_receipt_sha256
ON yoao_fixture_v1.fixture_runs
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_run_original_store();
-- aap-statement-break
CREATE FUNCTION yoao_fixture_v1.guard_maintenance_window()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.location_id IS DISTINCT FROM OLD.location_id
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
       OR NEW.binding_generation IS DISTINCT FROM OLD.binding_generation
       OR NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.initial_revision IS DISTINCT FROM OLD.initial_revision
       OR NEW.draining_revision IS DISTINCT FROM OLD.draining_revision
       OR (OLD.stopped_revision IS NOT NULL AND NEW.stopped_revision IS DISTINCT FROM OLD.stopped_revision)
       OR OLD.resumed_revision IS NOT NULL
       OR NEW.revision <> OLD.revision + 1
       OR NOT ((OLD.state = 'draining' AND NEW.state IN ('stopped', 'resumed'))
               OR (OLD.state = 'stopped' AND NEW.state = 'resumed')) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_maintenance_window_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_maintenance_window() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_maintenance_window_guard
BEFORE UPDATE ON yoao_fixture_v1.fixture_maintenance_windows
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_maintenance_window();
