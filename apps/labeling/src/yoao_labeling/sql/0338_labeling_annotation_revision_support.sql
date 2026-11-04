-- Preserve Mask revision capture inside the owner command search path.
CREATE OR REPLACE FUNCTION public.record_mask_annotation_revision(
    p_project_id uuid,
    p_task_id uuid,
    p_annotation_id uuid,
    p_annotation_version integer,
    p_geometry jsonb,
    p_source_kind text
) RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    INSERT INTO public.mask_annotation_revisions (
        id, project_id, task_id, annotation_id, annotation_version,
        geometry, geometry_digest, source_kind, expires_at
    ) VALUES (
        gen_random_uuid(),
        COALESCE(
            p_project_id,
            (SELECT project_id FROM public.tasks WHERE id = p_task_id)
        ),
        p_task_id,
        p_annotation_id,
        p_annotation_version,
        p_geometry,
        encode(public.digest(convert_to(p_geometry::text, 'UTF8'), 'sha256'), 'hex'),
        p_source_kind,
        'infinity'::timestamptz
    )
    ON CONFLICT (annotation_id, annotation_version) DO NOTHING;

    -- The newest 20 snapshots remain live regardless of age. Once a
    -- snapshot falls out of that window it expires no earlier than 30
    -- days after capture.
    WITH ranked AS (
        SELECT id,
               row_number() OVER (
                   PARTITION BY annotation_id
                   ORDER BY annotation_version DESC, created_at DESC, id DESC
               ) AS position
        FROM public.mask_annotation_revisions
        WHERE annotation_id = p_annotation_id
    )
    UPDATE public.mask_annotation_revisions AS revision
    SET expires_at = GREATEST(
        revision.created_at + interval '30 days',
        transaction_timestamp()
    )
    FROM ranked
    WHERE revision.id = ranked.id
      AND ranked.position > 20
      AND revision.expires_at = 'infinity'::timestamptz;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.capture_mask_annotation_revision()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    old_is_mask boolean;
    new_is_mask boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF COALESCE(NEW.geometry->>'type', '') IN (
            'raster_mask', 'video_mask', 'video_track_mask'
        ) AND (NEW.version IS NULL OR NEW.version < 1) THEN
            NEW.version := 1;
        END IF;
        RETURN NEW;
    END IF;

    old_is_mask := COALESCE(OLD.geometry->>'type', '') IN (
        'raster_mask', 'video_mask', 'video_track_mask'
    );

    IF TG_OP = 'DELETE' THEN
        IF old_is_mask THEN
            PERFORM public.record_mask_annotation_revision(
                OLD.project_id,
                OLD.task_id,
                OLD.id,
                OLD.version,
                OLD.geometry,
                OLD.source
            );
        END IF;
        RETURN OLD;
    END IF;

    new_is_mask := COALESCE(NEW.geometry->>'type', '') IN (
        'raster_mask', 'video_mask', 'video_track_mask'
    );
    IF old_is_mask OR new_is_mask THEN
        IF NEW.version IS NULL OR NEW.version <= OLD.version THEN
            NEW.version := OLD.version + 1;
        ELSIF NEW.version > OLD.version + 1 THEN
            RAISE EXCEPTION
                'mask annotation version must advance exactly once: % -> %',
                OLD.version,
                NEW.version;
        END IF;

        IF old_is_mask THEN
            PERFORM public.record_mask_annotation_revision(
                OLD.project_id,
                OLD.task_id,
                OLD.id,
                OLD.version,
                OLD.geometry,
                OLD.source
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
