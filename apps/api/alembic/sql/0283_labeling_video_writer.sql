-- Atomically publish the result of one claimed video execution.
-- Labeling owns the canonical Job and receipt; the narrow Jobs capability
-- holds the exact current execution proof through the same transaction.
CREATE TABLE aap_labeling_v1.video_writer_receipts (
    operation_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (
        request_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    outcome text NOT NULL CHECK (outcome IN ('completed','cancelled','failed')),
    status text NOT NULL CHECK (
        status IN ('pending_review','cancelled','failed')
    ),
    completed_at timestamptz NOT NULL,
    staged_sha256 text CHECK (
        staged_sha256 IS NULL OR staged_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    staged_byte_size bigint NOT NULL CHECK (staged_byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (job_id, run_id),
    CHECK (
        (staged_sha256 IS NULL AND staged_byte_size = 0)
        OR (staged_sha256 IS NOT NULL AND staged_byte_size > 0)
    ),
    CHECK (
        (outcome = 'completed' AND status = 'pending_review')
        OR (outcome = 'cancelled' AND status = 'cancelled')
        OR (outcome = 'failed' AND status = 'failed')
    )
);
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.video_writer_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_receipt_immutable()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='55000',
        MESSAGE='video writer receipts are immutable';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_receipt_immutable()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_writer_receipt_immutable
BEFORE UPDATE OR DELETE ON aap_labeling_v1.video_writer_receipts
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._video_writer_receipt_immutable();
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_reply(
    p_data jsonb,
    p_code text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-writer-data/v1','ok',true,
        'data',COALESCE(p_data,'{}'::jsonb)
    ) ELSE pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-writer-data/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code',p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_reply(jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_exact_keys(
    p_value jsonb,
    p_expected text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_typeof(p_value) = 'object'
       AND COALESCE((
            SELECT pg_catalog.array_agg(key ORDER BY key COLLATE "C")
            FROM pg_catalog.jsonb_object_keys(p_value) AS item(key)
       ),ARRAY[]::text[]) IS NOT DISTINCT FROM COALESCE((
            SELECT pg_catalog.array_agg(key ORDER BY key COLLATE "C")
            FROM pg_catalog.unnest(p_expected) AS item(key)
       ),ARRAY[]::text[])
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_exact_keys(jsonb,text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_digest_valid(p_digest text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT p_digest IS NOT NULL
       AND p_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_digest_valid(text)
    FROM PUBLIC;
-- aap-statement-break
-- Keep numeric instance ordering consistent across UTF8 database locales.
CREATE FUNCTION aap_labeling_v1._video_writer_instance_number(p_instance text)
RETURNS numeric
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_digits text;
BEGIN
    IF p_instance ~ '^[0-9]+$' THEN RETURN p_instance::numeric; END IF;
    v_digits := pg_catalog.translate(p_instance,'0123456789٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹߀߁߂߃߄߅߆߇߈߉०१२३४५६७८९০১২৩৪৫৬৭৮৯੦੧੨੩੪੫੬੭੮੯૦૧૨૩૪૫૬૭૮૯୦୧୨୩୪୫୬୭୮୯௦௧௨௩௪௫௬௭௮௯౦౧౨౩౪౫౬౭౮౯೦೧೨೩೪೫೬೭೮೯൦൧൨൩൪൫൬൭൮൯෦෧෨෩෪෫෬෭෮෯๐๑๒๓๔๕๖๗๘๙໐໑໒໓໔໕໖໗໘໙༠༡༢༣༤༥༦༧༨༩၀၁၂၃၄၅၆၇၈၉႐႑႒႓႔႕႖႗႘႙០១២៣៤៥៦៧៨៩᠐᠑᠒᠓᠔᠕᠖᠗᠘᠙᥆᥇᥈᥉᥊᥋᥌᥍᥎᥏᧐᧑᧒᧓᧔᧕᧖᧗᧘᧙᪀᪁᪂᪃᪄᪅᪆᪇᪈᪉᪐᪑᪒᪓᪔᪕᪖᪗᪘᪙᭐᭑᭒᭓᭔᭕᭖᭗᭘᭙᮰᮱᮲᮳᮴᮵᮶᮷᮸᮹᱀᱁᱂᱃᱄᱅᱆᱇᱈᱉᱐᱑᱒᱓᱔᱕᱖᱗᱘᱙꘠꘡꘢꘣꘤꘥꘦꘧꘨꘩꣐꣑꣒꣓꣔꣕꣖꣗꣘꣙꤀꤁꤂꤃꤄꤅꤆꤇꤈꤉꧐꧑꧒꧓꧔꧕꧖꧗꧘꧙꧰꧱꧲꧳꧴꧵꧶꧷꧸꧹꩐꩑꩒꩓꩔꩕꩖꩗꩘꩙꯰꯱꯲꯳꯴꯵꯶꯷꯸꯹０１２３４５６７８９𐒠𐒡𐒢𐒣𐒤𐒥𐒦𐒧𐒨𐒩𐴰𐴱𐴲𐴳𐴴𐴵𐴶𐴷𐴸𐴹𑁦𑁧𑁨𑁩𑁪𑁫𑁬𑁭𑁮𑁯𑃰𑃱𑃲𑃳𑃴𑃵𑃶𑃷𑃸𑃹𑄶𑄷𑄸𑄹𑄺𑄻𑄼𑄽𑄾𑄿𑇐𑇑𑇒𑇓𑇔𑇕𑇖𑇗𑇘𑇙𑋰𑋱𑋲𑋳𑋴𑋵𑋶𑋷𑋸𑋹𑑐𑑑𑑒𑑓𑑔𑑕𑑖𑑗𑑘𑑙𑓐𑓑𑓒𑓓𑓔𑓕𑓖𑓗𑓘𑓙𑙐𑙑𑙒𑙓𑙔𑙕𑙖𑙗𑙘𑙙𑛀𑛁𑛂𑛃𑛄𑛅𑛆𑛇𑛈𑛉𑜰𑜱𑜲𑜳𑜴𑜵𑜶𑜷𑜸𑜹𑣠𑣡𑣢𑣣𑣤𑣥𑣦𑣧𑣨𑣩𑥐𑥑𑥒𑥓𑥔𑥕𑥖𑥗𑥘𑥙𑱐𑱑𑱒𑱓𑱔𑱕𑱖𑱗𑱘𑱙𑵐𑵑𑵒𑵓𑵔𑵕𑵖𑵗𑵘𑵙𑶠𑶡𑶢𑶣𑶤𑶥𑶦𑶧𑶨𑶩𑽐𑽑𑽒𑽓𑽔𑽕𑽖𑽗𑽘𑽙𖩠𖩡𖩢𖩣𖩤𖩥𖩦𖩧𖩨𖩩𖫀𖫁𖫂𖫃𖫄𖫅𖫆𖫇𖫈𖫉𖭐𖭑𖭒𖭓𖭔𖭕𖭖𖭗𖭘𖭙𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿𞅀𞅁𞅂𞅃𞅄𞅅𞅆𞅇𞅈𞅉𞋰𞋱𞋲𞋳𞋴𞋵𞋶𞋷𞋸𞋹𞓰𞓱𞓲𞓳𞓴𞓵𞓶𞓷𞓸𞓹𞥐𞥑𞥒𞥓𞥔𞥕𞥖𞥗𞥘𞥙🯰🯱🯲🯳🯴🯵🯶🯷🯸🯹','01234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789');
    IF v_digits ~ '^[0-9]+$' THEN RETURN v_digits::numeric; END IF;
    IF p_instance <> ''
       AND pg_catalog.translate(p_instance,'0123456789²³¹٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹߀߁߂߃߄߅߆߇߈߉०१२३४५६७८९০১২৩৪৫৬৭৮৯੦੧੨੩੪੫੬੭੮੯૦૧૨૩૪૫૬૭૮૯୦୧୨୩୪୫୬୭୮୯௦௧௨௩௪௫௬௭௮௯౦౧౨౩౪౫౬౭౮౯೦೧೨೩೪೫೬೭೮೯൦൧൨൩൪൫൬൭൮൯෦෧෨෩෪෫෬෭෮෯๐๑๒๓๔๕๖๗๘๙໐໑໒໓໔໕໖໗໘໙༠༡༢༣༤༥༦༧༨༩၀၁၂၃၄၅၆၇၈၉႐႑႒႓႔႕႖႗႘႙፩፪፫፬፭፮፯፰፱០១២៣៤៥៦៧៨៩᠐᠑᠒᠓᠔᠕᠖᠗᠘᠙᥆᥇᥈᥉᥊᥋᥌᥍᥎᥏᧐᧑᧒᧓᧔᧕᧖᧗᧘᧙᧚᪀᪁᪂᪃᪄᪅᪆᪇᪈᪉᪐᪑᪒᪓᪔᪕᪖᪗᪘᪙᭐᭑᭒᭓᭔᭕᭖᭗᭘᭙᮰᮱᮲᮳᮴᮵᮶᮷᮸᮹᱀᱁᱂᱃᱄᱅᱆᱇᱈᱉᱐᱑᱒᱓᱔᱕᱖᱗᱘᱙⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉①②③④⑤⑥⑦⑧⑨⑴⑵⑶⑷⑸⑹⑺⑻⑼⒈⒉⒊⒋⒌⒍⒎⒏⒐⓪⓵⓶⓷⓸⓹⓺⓻⓼⓽⓿❶❷❸❹❺❻❼❽❾➀➁➂➃➄➅➆➇➈➊➋➌➍➎➏➐➑➒꘠꘡꘢꘣꘤꘥꘦꘧꘨꘩꣐꣑꣒꣓꣔꣕꣖꣗꣘꣙꤀꤁꤂꤃꤄꤅꤆꤇꤈꤉꧐꧑꧒꧓꧔꧕꧖꧗꧘꧙꧰꧱꧲꧳꧴꧵꧶꧷꧸꧹꩐꩑꩒꩓꩔꩕꩖꩗꩘꩙꯰꯱꯲꯳꯴꯵꯶꯷꯸꯹０１２３４５６７８９𐒠𐒡𐒢𐒣𐒤𐒥𐒦𐒧𐒨𐒩𐩀𐩁𐩂𐩃𐴰𐴱𐴲𐴳𐴴𐴵𐴶𐴷𐴸𐴹𐹠𐹡𐹢𐹣𐹤𐹥𐹦𐹧𐹨𑁒𑁓𑁔𑁕𑁖𑁗𑁘𑁙𑁚𑁦𑁧𑁨𑁩𑁪𑁫𑁬𑁭𑁮𑁯𑃰𑃱𑃲𑃳𑃴𑃵𑃶𑃷𑃸𑃹𑄶𑄷𑄸𑄹𑄺𑄻𑄼𑄽𑄾𑄿𑇐𑇑𑇒𑇓𑇔𑇕𑇖𑇗𑇘𑇙𑋰𑋱𑋲𑋳𑋴𑋵𑋶𑋷𑋸𑋹𑑐𑑑𑑒𑑓𑑔𑑕𑑖𑑗𑑘𑑙𑓐𑓑𑓒𑓓𑓔𑓕𑓖𑓗𑓘𑓙𑙐𑙑𑙒𑙓𑙔𑙕𑙖𑙗𑙘𑙙𑛀𑛁𑛂𑛃𑛄𑛅𑛆𑛇𑛈𑛉𑜰𑜱𑜲𑜳𑜴𑜵𑜶𑜷𑜸𑜹𑣠𑣡𑣢𑣣𑣤𑣥𑣦𑣧𑣨𑣩𑥐𑥑𑥒𑥓𑥔𑥕𑥖𑥗𑥘𑥙𑱐𑱑𑱒𑱓𑱔𑱕𑱖𑱗𑱘𑱙𑵐𑵑𑵒𑵓𑵔𑵕𑵖𑵗𑵘𑵙𑶠𑶡𑶢𑶣𑶤𑶥𑶦𑶧𑶨𑶩𑽐𑽑𑽒𑽓𑽔𑽕𑽖𑽗𑽘𑽙𖩠𖩡𖩢𖩣𖩤𖩥𖩦𖩧𖩨𖩩𖫀𖫁𖫂𖫃𖫄𖫅𖫆𖫇𖫈𖫉𖭐𖭑𖭒𖭓𖭔𖭕𖭖𖭗𖭘𖭙𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿𞅀𞅁𞅂𞅃𞅄𞅅𞅆𞅇𞅈𞅉𞋰𞋱𞋲𞋳𞋴𞋵𞋶𞋷𞋸𞋹𞓰𞓱𞓲𞓳𞓴𞓵𞓶𞓷𞓸𞓹𞥐𞥑𞥒𞥓𞥔𞥕𞥖𞥗𞥘𞥙🄀🄁🄂🄃🄄🄅🄆🄇🄈🄉🄊🯰🯱🯲🯳🯴🯵🯶🯷🯸🯹','') = '' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid numeric tracker instance';
    END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_instance_number(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_writer_source_map(
    p_annotation_id uuid,
    p_execution jsonb,
    p_results jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_sources jsonb := p_execution -> 'source_annotations';
    v_seeds jsonb := p_execution #> '{prompt_snapshot,seeds}';
    v_seed jsonb;
    v_row jsonb;
    v_map jsonb := '{}'::jsonb;
    v_instance text;
    v_source_id uuid;
    v_primary text;
    v_primary_candidate text;
    v_source_count integer;
    v_result_count integer;
    v_seen_annotation boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(v_sources) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_sources) > 256
       OR pg_catalog.jsonb_typeof(p_results) IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    SELECT pg_catalog.count(*) INTO v_source_count
    FROM pg_catalog.jsonb_array_elements(v_sources) AS source(value)
    WHERE pg_catalog.jsonb_typeof(source.value) = 'object'
      AND pg_catalog.jsonb_typeof(source.value -> 'annotation_id') = 'string'
      AND source.value ->> 'annotation_id'
            ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    IF v_source_count <> pg_catalog.jsonb_array_length(v_sources) THEN
        RETURN NULL;
    END IF;
    IF v_seeds IS NULL OR v_seeds IN ('null'::jsonb,'false'::jsonb,'0'::jsonb,'""'::jsonb,'{}'::jsonb) THEN
        v_seeds := '[]'::jsonb;
    END IF;
    IF pg_catalog.jsonb_typeof(v_seeds) IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    FOR v_seed IN SELECT item.value
        FROM pg_catalog.jsonb_array_elements(v_seeds) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_seed) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        IF v_seed -> 'source_annotation_id' IS NULL
           OR v_seed -> 'source_annotation_id' = 'null'::jsonb THEN
            CONTINUE;
        END IF;
        IF pg_catalog.jsonb_typeof(v_seed -> 'obj_id') NOT IN ('string','number')
           OR pg_catalog.jsonb_typeof(v_seed -> 'source_annotation_id') <> 'string'
           OR (v_seed ->> 'source_annotation_id') !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN NULL;
        END IF;
        v_instance := v_seed ->> 'obj_id';
        IF v_instance IS NULL OR v_instance = '' OR pg_catalog.char_length(v_instance) > 255 THEN
            RETURN NULL;
        END IF;
        v_source_id := (v_seed ->> 'source_annotation_id')::uuid;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_sources) AS source(value)
            WHERE source.value ->> 'annotation_id' = v_source_id::text
        ) THEN
            RETURN NULL;
        END IF;
        IF v_map ? v_instance
           AND v_map ->> v_instance IS DISTINCT FROM v_source_id::text THEN
            RETURN NULL;
        END IF;
        v_map := v_map || pg_catalog.jsonb_build_object(v_instance,v_source_id);
    END LOOP;
    IF p_annotation_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_each_text(v_map) AS entry(key,value)
            WHERE entry.value = p_annotation_id::text
        ) INTO v_seen_annotation;
        IF NOT v_seen_annotation THEN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_sources) AS source(value)
                WHERE source.value ->> 'annotation_id' = p_annotation_id::text
            ) THEN
                RETURN NULL;
            END IF;
            SELECT item.value ->> 'instance_id' INTO v_primary
            FROM pg_catalog.jsonb_array_elements(p_results) WITH ORDINALITY AS item(value,ordinality)
            WHERE item.value -> 'primary' = 'true'::jsonb
              AND pg_catalog.jsonb_typeof(item.value -> 'instance_id') = 'string'
              AND item.value ->> 'instance_id' IS NOT NULL
            ORDER BY item.ordinality LIMIT 1;
            IF v_primary IS NULL THEN
                SELECT item.value ->> 'instance_id' INTO v_primary_candidate
                FROM pg_catalog.jsonb_array_elements(p_results) WITH ORDINALITY AS item(value,ordinality)
                WHERE pg_catalog.jsonb_typeof(item.value -> 'instance_id') = 'string'
                  AND item.value ->> 'instance_id' IS NOT NULL
                ORDER BY CASE WHEN aap_labeling_v1._video_writer_instance_number(
                                  item.value ->> 'instance_id') IS NOT NULL THEN 0 ELSE 1 END,
                         aap_labeling_v1._video_writer_instance_number(item.value ->> 'instance_id'),
                         CASE WHEN aap_labeling_v1._video_writer_instance_number(
                                  item.value ->> 'instance_id') IS NULL
                              THEN item.value ->> 'instance_id' END COLLATE "C",
                         item.ordinality
                LIMIT 1;
            ELSE
                v_primary_candidate := v_primary;
            END IF;
            SELECT item.value ->> 'instance_id' INTO v_primary
            FROM pg_catalog.jsonb_array_elements(p_results) WITH ORDINALITY AS item(value,ordinality)
            WHERE (item.value -> 'instance_id'='null'::jsonb
                   OR item.value ->> 'instance_id' IS NOT DISTINCT FROM v_primary_candidate)
            ORDER BY item.ordinality LIMIT 1;
            IF v_primary IS NULL THEN v_primary := '1'; END IF;
            IF v_map ? v_primary THEN RETURN NULL; END IF;
            v_map := v_map || pg_catalog.jsonb_build_object(
                v_primary,p_annotation_id
            );
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_each_text(v_map) AS entry(key,value)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_sources) AS source(value)
            WHERE source.value ->> 'annotation_id' = entry.value
        )
    ) THEN
        RETURN NULL;
    END IF;
    RETURN v_map;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_source_map(
    uuid,jsonb,jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_mask_epochs(p_object_keys jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_keys text[] := ARRAY[]::text[];
    v_key text;
    v_batch jsonb;
    v_reply jsonb;
    v_items jsonb;
    v_item jsonb;
    v_epochs jsonb := '{}'::jsonb;
    v_expected_count integer;
    v_offset integer := 0;
    v_batch_count integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p_object_keys) IS DISTINCT FROM 'array' THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_request');
    END IF;
    FOR v_item IN SELECT item.value
        FROM pg_catalog.jsonb_array_elements(p_object_keys) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'string' THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_request');
        END IF;
        v_key := v_item #>> '{}';
        IF NOT aap_labeling_v1._mask_gc_key_valid(v_key)
           OR v_key = ANY(v_keys) THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_request');
        END IF;
        IF cardinality(v_keys) > 0
           AND v_keys[cardinality(v_keys)] COLLATE "C" >= v_key COLLATE "C" THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_request');
        END IF;
        v_keys := pg_catalog.array_append(v_keys,v_key);
    END LOOP;
    v_expected_count := cardinality(v_keys);
    WHILE v_offset < v_expected_count LOOP
        v_batch_count := LEAST(1000,v_expected_count-v_offset);
        SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(v_keys[index]) ORDER BY index)
        INTO v_batch
        FROM pg_catalog.generate_series(v_offset+1,v_offset+v_batch_count) AS item(index);
        v_reply := aap_labeling_v1.begin_mask_content_write(
            pg_catalog.jsonb_build_object('object_keys',v_batch)
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-mask-gc/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_key := v_reply #>> '{error,code}';
            RETURN pg_catalog.jsonb_build_object(
                'ok',false,
                'code',CASE WHEN v_key='content_deleting'
                            THEN 'mask_content_deleting' ELSE 'invalid_response' END
            );
        END IF;
        v_items := v_reply #> '{data,items}';
        IF pg_catalog.jsonb_typeof(v_items) IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_items) <> v_batch_count THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
        END IF;
        FOR v_item IN SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_items) AS item(value)
        LOOP
            IF NOT aap_labeling_v1._video_writer_exact_keys(
                v_item,ARRAY['object_key','epoch']
            ) OR pg_catalog.jsonb_typeof(v_item -> 'object_key') <> 'string'
              OR pg_catalog.jsonb_typeof(v_item -> 'epoch') <> 'number'
              OR (v_item ->> 'epoch') !~ '^(0|[1-9][0-9]*)$'
              OR pg_catalog.char_length(v_item ->> 'epoch') > 19 THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
            END IF;
            v_key := v_item ->> 'object_key';
            IF NOT v_key = ANY(v_keys)
               OR v_epochs ? v_key THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
            END IF;
            v_epochs := v_epochs || pg_catalog.jsonb_build_object(
                v_key,(v_item ->> 'epoch')::bigint
            );
        END LOOP;
        v_offset := v_offset + v_batch_count;
    END LOOP;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_epochs))
         <> v_expected_count THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
    END IF;
    RETURN pg_catalog.jsonb_build_object('ok',true,'epochs',v_epochs);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN pg_catalog.jsonb_build_object('ok',false,'code','execution_busy');
WHEN OTHERS THEN
    RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_mask_epochs(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_facts(
    p_job jsonb,
    p_requested_outcome text,
    p_expected_input jsonb,
    p_plan jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_effective text;
    v_cancel_seen boolean;
    v_expected jsonb := p_expected_input;
    v_input_reply jsonb;
    v_input jsonb;
    v_evidence jsonb;
    v_acceptance jsonb;
    v_plan jsonb := p_plan;
    v_job_id uuid;
    v_task_id uuid;
    v_actor_id uuid;
    v_input_id uuid;
    v_request_digest text;
    v_plan_digest text;
    v_source_version text;
    v_source_sha256 text;
    v_need_stage boolean := false;
    v_error text;
    v_facts jsonb;
BEGIN
    v_job_id := (p_job ->> 'job_id')::uuid;
    v_task_id := (p_job ->> 'task_id')::uuid;
    IF p_requested_outcome='failed' THEN
        -- Failure cleanup has claim authority and must not reauthorize staging.
        v_plan := 'null'::jsonb;
    END IF;
    v_cancel_seen := p_job ->> 'status' = 'cancelled'
       OR p_job -> 'cancel_requested_at' IS DISTINCT FROM 'null'::jsonb;
    IF v_cancel_seen THEN
        v_effective := 'cancelled';
    ELSE
        v_effective := p_requested_outcome;
    END IF;
    IF p_requested_outcome = 'cancelled' AND NOT v_cancel_seen THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'job_cancelled');
    END IF;
    IF p_job ->> 'status' NOT IN ('queued','running','cancelled') THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'job_not_active');
    END IF;
    IF v_plan IS NULL THEN v_plan := 'null'::jsonb; END IF;
    IF v_plan <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(v_plan) IS DISTINCT FROM 'object'
           OR v_plan ->> 'schema' IS DISTINCT FROM 'aap-video-execution-plan/v1'
           OR v_plan ->> 'request_id' IS DISTINCT FROM v_job_id::text
           OR v_plan ->> 'task_id' IS DISTINCT FROM v_task_id::text
           OR v_plan ->> 'job_kind' IS DISTINCT FROM p_job ->> 'job_kind'
           OR v_plan ->> 'direction' IS DISTINCT FROM p_job ->> 'direction'
           OR v_plan ->> 'from_frame' IS DISTINCT FROM p_job ->> 'from_frame'
           OR v_plan ->> 'to_frame' IS DISTINCT FROM p_job ->> 'to_frame'
           OR v_plan ->> 'correction_frame' IS DISTINCT FROM p_job ->> 'correction_frame'
           OR v_plan ->> 'segment_id' IS DISTINCT FROM p_job ->> 'segment_id'
           OR NOT aap_labeling_v1._video_writer_digest_valid(v_plan ->> 'plan_digest')
           OR NOT aap_labeling_v1._video_writer_digest_valid(v_plan ->> 'source_plan_digest')
           OR NOT aap_labeling_v1._video_writer_digest_valid(v_plan ->> 'request_digest')
           OR pg_catalog.jsonb_typeof(v_plan -> 'input_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_plan #> '{source,source_version_id}')
                IS DISTINCT FROM 'string'
           OR NOT aap_labeling_v1._video_writer_digest_valid(
                v_plan #>> '{source,source_sha256}'
              ) THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'plan_not_frozen');
        END IF;
    END IF;
    IF p_requested_outcome = 'completed' AND v_plan = 'null'::jsonb THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'plan_not_frozen');
    END IF;
    IF p_requested_outcome = 'completed' AND v_expected = 'null'::jsonb THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'input_unavailable');
    END IF;
    IF v_expected IS NULL THEN v_expected := 'null'::jsonb; END IF;
    IF v_expected <> 'null'::jsonb THEN
        IF NOT aap_labeling_v1._video_writer_exact_keys(
            v_expected,ARRAY[
                'input_id','request_digest','plan_digest','source_version_id','source_sha256'
            ]
        ) OR pg_catalog.jsonb_typeof(v_expected -> 'input_id') <> 'string'
          OR pg_catalog.jsonb_typeof(v_expected -> 'request_digest') <> 'string'
          OR pg_catalog.jsonb_typeof(v_expected -> 'plan_digest') <> 'string'
          OR pg_catalog.jsonb_typeof(v_expected -> 'source_version_id') <> 'string'
          OR pg_catalog.jsonb_typeof(v_expected -> 'source_sha256') <> 'string'
          OR NOT aap_labeling_v1._video_writer_digest_valid(
                v_expected ->> 'request_digest'
             )
          OR NOT aap_labeling_v1._video_writer_digest_valid(
                v_expected ->> 'plan_digest'
             )
          OR NOT aap_labeling_v1._video_writer_digest_valid(
                v_expected ->> 'source_sha256'
             )
          OR pg_catalog.char_length(v_expected ->> 'source_version_id') NOT BETWEEN 1 AND 255
          OR pg_catalog.btrim(v_expected ->> 'source_version_id') = ''
          OR v_expected ->> 'source_version_id' = 'null' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        BEGIN
            v_input_id := (v_expected ->> 'input_id')::uuid;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END;
        IF v_input_id::text IS DISTINCT FROM v_expected ->> 'input_id' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        v_request_digest := v_expected ->> 'request_digest';
        v_plan_digest := v_expected ->> 'plan_digest';
        v_source_version := v_expected ->> 'source_version_id';
        v_source_sha256 := v_expected ->> 'source_sha256';
    ELSIF v_effective = 'cancelled'
       AND p_job ->> 'job_kind' = 'tracking'
       AND v_plan <> 'null'::jsonb THEN
        v_expected := pg_catalog.jsonb_build_object(
            'input_id',v_plan -> 'input_id',
            'request_digest',v_plan -> 'request_digest',
            'plan_digest',v_plan -> 'source_plan_digest',
            'source_version_id',v_plan #> '{source,source_version_id}',
            'source_sha256',v_plan #> '{source,source_sha256}'
        );
        v_input_id := (v_plan ->> 'input_id')::uuid;
        v_request_digest := v_plan ->> 'request_digest';
        v_plan_digest := v_plan ->> 'source_plan_digest';
        v_source_version := v_plan #>> '{source,source_version_id}';
        v_source_sha256 := v_plan #>> '{source,source_sha256}';
    END IF;
    IF v_expected <> 'null'::jsonb AND v_plan <> 'null'::jsonb THEN
        IF v_plan ->> 'input_id' IS DISTINCT FROM v_input_id::text
           OR v_plan ->> 'request_digest' IS DISTINCT FROM v_request_digest
           OR v_plan ->> 'source_plan_digest' IS DISTINCT FROM v_plan_digest
           OR v_plan #>> '{source,source_version_id}' IS DISTINCT FROM v_source_version
           OR v_plan #>> '{source,source_sha256}' IS DISTINCT FROM v_source_sha256 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'input_unavailable');
        END IF;
    ELSIF p_requested_outcome='completed' THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'plan_not_frozen');
    END IF;
    v_need_stage := p_requested_outcome<>'failed'
        AND NOT (v_effective='cancelled' AND p_job ->> 'job_kind'='correction')
        AND (p_requested_outcome='completed'
        OR (v_effective='cancelled' AND p_job ->> 'job_kind'='tracking'
            AND (p_expected_input <> 'null'::jsonb
                 OR p_job -> 'has_track_results'='true'::jsonb)));
    IF v_need_stage THEN
        IF v_expected = 'null'::jsonb THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'input_unavailable');
        END IF;
        IF p_job -> 'created_by' IS NULL OR p_job -> 'created_by'='null'::jsonb THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'permission_denied');
        END IF;
        v_actor_id := (p_job ->> 'created_by')::uuid;
        v_input_reply := aap_labeling_v1.lock_video_tracker_input_for_stage(
            v_actor_id,v_job_id,v_task_id,v_input_id,v_request_digest,v_plan_digest
        );
        IF v_input_reply IS NULL
           OR v_input_reply ->> 'schema' IS DISTINCT FROM
                'aap-labeling-video-tracker-inputs/v1'
           OR pg_catalog.jsonb_typeof(v_input_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_response');
        END IF;
        IF v_input_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_error := v_input_reply #>> '{error,code}';
            RETURN aap_labeling_v1._video_writer_reply(NULL,CASE
                WHEN v_error IN ('task_busy','input_busy','account_busy') THEN 'input_busy'
                WHEN v_error IN ('permission_denied','account_not_active',
                                 'invalid_account_role','task_not_visible')
                    THEN 'permission_denied'
                WHEN v_error='invalid_response' THEN 'invalid_response'
                ELSE 'input_unavailable' END);
        END IF;
        v_input := v_input_reply -> 'data' -> 'input';
        v_evidence := v_input_reply -> 'data' -> 'evidence';
        IF pg_catalog.jsonb_typeof(v_input) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_evidence) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_input #> '{scope,execution}')
                IS DISTINCT FROM 'object'
           OR v_input ->> 'input_id' IS DISTINCT FROM v_input_id::text
           OR v_evidence ->> 'input_id' IS DISTINCT FROM v_input_id::text
           OR v_evidence ->> 'request_digest' IS DISTINCT FROM v_request_digest
           OR v_evidence ->> 'plan_digest' IS DISTINCT FROM v_plan_digest
           OR v_evidence ->> 'source_version_id' IS DISTINCT FROM v_source_version
           OR v_evidence ->> 'source_sha256' IS DISTINCT FROM v_source_sha256 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'input_unavailable');
        END IF;
        v_acceptance := pg_catalog.jsonb_build_object(
            'execution',v_input #> '{scope,execution}',
            'evidence',v_evidence
        );
    END IF;
    v_facts := pg_catalog.jsonb_build_object(
        'job',pg_catalog.jsonb_build_object(
            'job_id',p_job -> 'job_id',
            'task_id',p_job -> 'task_id',
            'dataset_item_id',p_job -> 'dataset_item_id',
            'annotation_id',p_job -> 'annotation_id',
            'created_by',p_job -> 'created_by',
            'job_kind',p_job -> 'job_kind',
            'from_frame',p_job -> 'from_frame',
            'to_frame',p_job -> 'to_frame',
            'direction',p_job -> 'direction',
            'status',p_job -> 'status',
            'event_channel',p_job -> 'event_channel'
        ),
        'effective_outcome',v_effective,
        'acceptance',v_acceptance,
        'execution_plan',v_plan
    );
    RETURN aap_labeling_v1._video_writer_reply(
        pg_catalog.jsonb_build_object('facts',v_facts)
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_writer_reply(NULL,'input_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_labeling_v1._video_writer_reply(NULL,'input_unavailable');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_facts(jsonb,text,jsonb,jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.video_writer_authority(
    p_action text,
    p_arguments jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_keys text[];
    v_proof jsonb;
    v_reply jsonb;
    v_plan jsonb;
    v_planned jsonb;
    v_window aap_jobs_v1.video_execution_windows%ROWTYPE;
    v_manifest jsonb;
    v_attempt jsonb;
    v_part_bytes bytea;
    v_bytes bytea := ''::bytea;
    v_payload_text text;
    v_payload jsonb;
    v_part_digest text;
    v_index integer;
    v_count integer;
    v_total bigint := 0;
    v_payload_size bigint;
    v_job_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_lease_token uuid;
    v_generation bigint;
    v_ordinal integer;
    v_outcome text;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments::text) > 135266304
       OR pg_catalog.jsonb_typeof(p_arguments) IS DISTINCT FROM 'object' THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    IF p_action IN ('inspect','has_track_results') THEN
        v_keys := ARRAY[]::text[];
    ELSIF p_action = 'read_window' THEN
        v_keys := ARRAY['window_ordinal'];
    ELSIF p_action IN ('guard_stage','guard_cancel','guard_fail') THEN
        v_keys := ARRAY[]::text[];
    ELSIF p_action IN ('finish_completed','finish_cancelled','finish_failed') THEN
        v_keys := ARRAY[]::text[];
    ELSE
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    v_keys := ARRAY[
        'job_id','run_id','worker_id','generation','lease_token'
    ] || v_keys;
    IF (SELECT pg_catalog.array_agg(key ORDER BY key COLLATE "C")
        FROM pg_catalog.jsonb_object_keys(p_arguments) AS item(key))
       IS DISTINCT FROM (SELECT pg_catalog.array_agg(key ORDER BY key COLLATE "C")
        FROM pg_catalog.unnest(v_keys) AS item(key)) THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        v_job_id := aap_jobs_v1._video_execution_uuid(p_arguments -> 'job_id');
        v_run_id := aap_jobs_v1._video_execution_uuid(p_arguments -> 'run_id');
        v_worker_id := aap_jobs_v1._video_execution_uuid(p_arguments -> 'worker_id');
        v_lease_token := aap_jobs_v1._video_execution_uuid(p_arguments -> 'lease_token');
        v_generation := aap_jobs_v1._video_checkpoint_integer(
            p_arguments -> 'generation',1,9223372036854775807
        );
        IF v_generation IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        IF p_action = 'read_window' THEN
            v_ordinal := aap_jobs_v1._video_checkpoint_integer(
                p_arguments -> 'window_ordinal',0,8191
            );
            IF v_ordinal IS NULL THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
            END IF;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END;
    v_proof := pg_catalog.jsonb_build_object(
        'job_id',v_job_id,'run_id',v_run_id,'worker_id',v_worker_id,
        'generation',v_generation,'lease_token',v_lease_token
    );

    IF p_action IN ('inspect','read_window','has_track_results') THEN
        v_reply := aap_jobs_v1.video_execution_checkpoint_step(
            'read_plan',v_proof::text
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
           OR v_reply ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN COALESCE(v_reply,
                aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_response'));
        END IF;
        IF p_action = 'inspect' THEN RETURN v_reply; END IF;
        v_plan := v_reply -> 'plan';
        IF p_action='has_track_results' THEN
            FOR v_planned IN SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                WHERE item.value ->> 'phase'='track'
            LOOP
                v_reply := aap_jobs_v1.video_writer_authority(
                    'read_window',v_proof || pg_catalog.jsonb_build_object(
                        'window_ordinal',v_planned -> 'ordinal'
                    )
                );
                IF v_reply ->> 'status' IS DISTINCT FROM 'ok' THEN RETURN v_reply; END IF;
                IF v_reply -> 'checkpoint' <> 'null'::jsonb THEN
                    v_payload := (v_reply #>> '{checkpoint,payload_json}')::jsonb;
                    IF pg_catalog.jsonb_array_length(v_payload -> 'frame_results')>0 THEN
                        RETURN aap_jobs_v1._video_checkpoint_reply(
                            pg_catalog.jsonb_build_object('has_track_results',true)
                        );
                    END IF;
                END IF;
            END LOOP;
            RETURN aap_jobs_v1._video_checkpoint_reply(
                pg_catalog.jsonb_build_object('has_track_results',false)
            );
        END IF;
        IF v_plan IS NULL OR v_plan = 'null'::jsonb THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_not_frozen');
        END IF;
        SELECT planned_window.value INTO v_planned
        FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS planned_window(value)
        WHERE (planned_window.value ->> 'ordinal')::integer = v_ordinal;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        v_attempt := NULL;
        SELECT pg_catalog.jsonb_build_object(
            'generation',attempt.generation,'window_ordinal',attempt.window_ordinal,
            'mode',attempt.mode,'state',attempt.state
        ) INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation AND attempt.window_ordinal=v_ordinal;
        v_manifest := NULL;
        SELECT result.* INTO v_window
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.window_ordinal=v_ordinal;
        IF FOUND THEN
            v_manifest := v_window.manifest;
            IF NOT aap_jobs_v1._video_checkpoint_manifest_valid(v_manifest)
               OR v_manifest ->> 'request_id' IS DISTINCT FROM v_job_id::text
               OR v_manifest ->> 'input_id' IS DISTINCT FROM v_plan ->> 'input_id'
               OR v_manifest ->> 'plan_digest' IS DISTINCT FROM v_plan ->> 'plan_digest'
               OR v_manifest ->> 'window_ordinal' IS DISTINCT FROM v_ordinal::text
               OR v_manifest ->> 'window_digest' IS DISTINCT FROM v_planned ->> 'window_digest'
               OR v_manifest ->> 'context_span_digest'
                    IS DISTINCT FROM v_planned #>> '{context_span,span_digest}'
               OR v_manifest ->> 'phase' IS DISTINCT FROM v_planned ->> 'phase'
 THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
            END IF;
            v_count := aap_jobs_v1._video_checkpoint_integer(
                v_manifest -> 'payload_part_count',1,128
            );
            v_payload_size := aap_jobs_v1._video_checkpoint_integer(
                v_manifest -> 'payload_byte_size',1,67108864
            );
            IF v_count IS NULL OR v_payload_size IS NULL
               OR v_window.checkpoint_id IS DISTINCT FROM v_manifest ->> 'checkpoint_id'
               OR v_window.manifest_digest IS DISTINCT FROM v_manifest ->> 'manifest_digest' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
            END IF;
            v_total := 0;
            v_bytes := ''::bytea;
            FOR v_index IN 0..v_count-1 LOOP
                SELECT part.part_bytes,part.part_digest
                INTO v_part_bytes,v_part_digest
                FROM aap_jobs_v1.video_execution_window_parts AS part
                WHERE part.job_id=v_job_id AND part.run_id=v_run_id
                  AND part.window_ordinal=v_ordinal AND part.part_index=v_index;
                IF NOT FOUND OR pg_catalog.encode(
                       public.digest(v_part_bytes,'sha256'),'hex'
                   ) IS DISTINCT FROM v_part_digest
                   OR pg_catalog.octet_length(v_part_bytes) NOT BETWEEN 1 AND 524288 THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
                END IF;
                IF pg_catalog.octet_length(v_part_bytes) <>
                    LEAST(524288::bigint,v_payload_size-v_index::bigint*524288) THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
                END IF;
                v_total := v_total + pg_catalog.octet_length(v_part_bytes);
                IF v_total > 67108864 THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
                END IF;
                v_bytes := v_bytes || v_part_bytes;
            END LOOP;
            IF v_total IS DISTINCT FROM (v_manifest ->> 'payload_byte_size')::bigint
               OR pg_catalog.encode(public.digest(v_bytes,'sha256'),'hex')
                    IS DISTINCT FROM v_manifest ->> 'payload_digest' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
            END IF;
            BEGIN
                v_payload_text := pg_catalog.convert_from(v_bytes,'UTF8');
                v_payload := v_payload_text::jsonb;
            EXCEPTION WHEN OTHERS THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
            END;
            IF pg_catalog.octet_length(v_payload_text) <> v_total
               OR pg_catalog.jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
               OR v_payload ->> 'schema' IS DISTINCT FROM
                    'aap-video-window-result-content/v1'
               OR v_payload ->> 'checkpoint_id' IS DISTINCT FROM v_manifest ->> 'checkpoint_id'
               OR v_payload ->> 'request_id' IS DISTINCT FROM v_job_id::text
               OR v_payload ->> 'input_id' IS DISTINCT FROM v_plan ->> 'input_id'
               OR v_payload ->> 'plan_digest' IS DISTINCT FROM v_plan ->> 'plan_digest'
               OR v_payload ->> 'window_ordinal' IS DISTINCT FROM v_ordinal::text
               OR v_payload ->> 'window_digest' IS DISTINCT FROM v_planned ->> 'window_digest'
               OR v_payload ->> 'context_span_digest'
                    IS DISTINCT FROM v_planned #>> '{context_span,span_digest}'
               OR v_payload ->> 'phase' IS DISTINCT FROM v_planned ->> 'phase'
               OR v_payload ->> 'branch' IS DISTINCT FROM v_planned ->> 'branch'
               OR v_payload ->> 'result_digest' IS DISTINCT FROM v_manifest ->> 'result_digest'
               OR pg_catalog.jsonb_typeof(v_payload -> 'frame_results') IS DISTINCT FROM 'array' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
            END IF;
        END IF;
        v_reply := aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'checkpoint',CASE WHEN v_manifest IS NULL THEN NULL ELSE
                pg_catalog.jsonb_build_object(
                    'manifest',v_manifest,'payload_json',v_payload_text
                ) END,
            'attempt',v_attempt
        ));
        IF pg_catalog.octet_length(v_reply::text) > 135266304 THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
        END IF;
        RETURN v_reply;
    END IF;

    IF p_action IN ('guard_stage','guard_cancel','guard_fail') THEN
        v_reply := aap_jobs_v1.video_execution_step(
            'guard',(v_proof || pg_catalog.jsonb_build_object(
                'purpose',CASE p_action
                    WHEN 'guard_stage' THEN 'stage'
                    WHEN 'guard_cancel' THEN 'cancel'
                    ELSE 'fail' END
            ))::text
        );
        RETURN v_reply;
    END IF;

    IF p_action IN ('finish_completed','finish_cancelled','finish_failed') THEN
        v_outcome := CASE p_action
            WHEN 'finish_completed' THEN 'completed'
            WHEN 'finish_cancelled' THEN 'cancelled'
            ELSE 'failed' END;
        RETURN aap_jobs_v1.video_execution_checkpoint_step(
            'finish',(v_proof || pg_catalog.jsonb_build_object(
                'outcome',v_outcome
            ))::text
        );
    END IF;
    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'execution_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'storage_unavailable');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_writer_authority(text,jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_authority_code(p_code text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT CASE WHEN p_code IN (
        'invalid_request','invalid_response','storage_unavailable',
        'operation_conflict','lease_lost','realm_mismatch','execution_busy',
        'recovery_required','job_missing','job_not_active','job_cancelled',
        'input_unavailable','input_busy','permission_denied','candidate_invalid',
        'source_map_invalid','staged_result_too_large','mask_content_deleting',
        'mask_content_epoch_changed','plan_not_frozen','checkpoint_incomplete',
        'finish_conflict'
    ) THEN p_code
    WHEN p_code IN ('scope_unavailable','route_unavailable') THEN 'recovery_required'
    WHEN p_code IN ('manifest_invalid','manifest_mismatch','checkpoint_missing',
                    'part_missing','part_invalid','part_conflict','window_mismatch',
                    'window_out_of_order','span_incomplete','span_mismatch')
        THEN 'checkpoint_incomplete'
    WHEN p_code IN ('input_busy','task_busy','account_busy') THEN 'input_busy'
    WHEN p_code IN ('content_deleting') THEN 'mask_content_deleting'
    WHEN p_code IN ('content_epoch_changed') THEN 'mask_content_epoch_changed'
    ELSE 'invalid_response' END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_authority_code(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_writer_stage_validate(
    p_proof jsonb,
    p_job jsonb,
    p_requested_outcome text,
    p_effective_outcome text,
    p_facts jsonb,
    p_stage jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_plan jsonb := p_facts -> 'execution_plan';
    v_acceptance jsonb := p_facts -> 'acceptance';
    v_windows jsonb;
    v_window jsonb;
    v_reply jsonb;
    v_checkpoint jsonb;
    v_manifest jsonb;
    v_payload jsonb;
    v_attempt jsonb;
    v_canonical jsonb := '[]'::jsonb;
    v_rows jsonb;
    v_map jsonb;
    v_keys jsonb;
    v_key text;
    v_boundary jsonb;
    v_boundaries jsonb := '[]'::jsonb;
    v_gap boolean := false;
    v_first_missing jsonb;
    v_first_missing_attempt jsonb;
    v_phase text;
    v_branch text;
    v_start bigint;
    v_end bigint;
    v_track_ordinal bigint := 0;
    v_prev_frame bigint;
    v_frame bigint;
    v_index bigint;
    v_prefix_count bigint;
    v_result_count bigint;
    v_tail_branch text;
BEGIN
    IF p_stage IS NULL OR p_stage='null'::jsonb THEN
        IF p_effective_outcome='completed' THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
        END IF;
        IF p_job ->> 'job_kind'='correction' AND p_effective_outcome='cancelled' THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok',true,'source_map',NULL,'object_keys','[]'::jsonb
            );
        END IF;
        IF p_effective_outcome='cancelled'
           AND p_job ->> 'job_kind'='tracking'
           AND v_plan IS NOT NULL AND v_plan<>'null'::jsonb THEN
            v_gap := false;
            FOR v_window IN SELECT item.value
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows')
                    WITH ORDINALITY AS item(value,ordinality)
                ORDER BY item.ordinality
            LOOP
                v_reply := aap_jobs_v1.video_writer_authority(
                    'read_window',p_proof || pg_catalog.jsonb_build_object(
                        'window_ordinal',v_window -> 'ordinal'
                    )
                );
                IF v_reply IS NULL
                   OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
                   OR v_reply ->> 'status' IS DISTINCT FROM 'ok' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'ok',false,
                        'code',aap_labeling_v1._video_writer_authority_code(
                            v_reply ->> 'reason'
                        )
                    );
                END IF;
                v_checkpoint := v_reply -> 'checkpoint';
                IF v_checkpoint IS NULL OR v_checkpoint='null'::jsonb THEN
                    v_gap := true;
                ELSE
                    IF v_gap THEN
                        RETURN pg_catalog.jsonb_build_object(
                            'ok',false,'code','checkpoint_incomplete'
                        );
                    END IF;
                    BEGIN
                        v_payload := (v_checkpoint ->> 'payload_json')::jsonb;
                    EXCEPTION WHEN OTHERS THEN
                        RETURN pg_catalog.jsonb_build_object(
                            'ok',false,'code','checkpoint_incomplete'
                        );
                    END;
                    IF v_window ->> 'phase'='track'
                       AND (pg_catalog.jsonb_typeof(v_payload -> 'frame_results')
                                IS DISTINCT FROM 'array'
                            OR pg_catalog.jsonb_array_length(v_payload -> 'frame_results')>0) THEN
                        RETURN pg_catalog.jsonb_build_object(
                            'ok',false,'code','checkpoint_incomplete'
                        );
                    END IF;
                END IF;
            END LOOP;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'ok',true,'source_map',NULL,'object_keys','[]'::jsonb
        );
    END IF;
    IF p_effective_outcome NOT IN ('completed','cancelled')
       OR (p_effective_outcome='cancelled' AND p_requested_outcome='cancelled'
           AND p_job ->> 'job_kind'='correction')
       OR v_plan IS NULL OR v_plan='null'::jsonb
       OR v_acceptance IS NULL OR v_acceptance='null'::jsonb
       OR NOT aap_labeling_v1._video_writer_exact_keys(
            p_stage,CASE WHEN p_stage ? 'tracker_context_boundaries' THEN
                ARRAY['results','grid_step','output_geometry','source_instance_map',
                      'tracker_input_evidence','tracker_context_boundaries']
            ELSE ARRAY['results','grid_step','output_geometry','source_instance_map',
                       'tracker_input_evidence'] END
          )
       OR pg_catalog.jsonb_typeof(p_stage -> 'results') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_stage -> 'grid_step') IS DISTINCT FROM 'number'
       OR (p_stage ->> 'grid_step') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_stage -> 'output_geometry') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_stage -> 'source_instance_map') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_stage -> 'tracker_input_evidence') IS DISTINCT FROM 'object'
       OR p_stage -> 'tracker_input_evidence' IS DISTINCT FROM v_acceptance -> 'evidence'
       OR (p_stage ->> 'grid_step')::bigint
            IS DISTINCT FROM (v_plan #>> '{output,grid_step}')::bigint
       OR p_stage ->> 'output_geometry' IS DISTINCT FROM v_plan #>> '{output,geometry}' THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
    END IF;
    IF p_stage ? 'tracker_context_boundaries' THEN
        IF p_requested_outcome <> 'completed'
           OR pg_catalog.jsonb_typeof(p_stage -> 'tracker_context_boundaries')
                IS DISTINCT FROM 'array' THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
        END IF;
    END IF;
    v_rows := p_stage -> 'results';
    IF p_effective_outcome='completed' AND p_job ->> 'job_kind'='correction'
       AND pg_catalog.jsonb_array_length(v_rows)=0 THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
    END IF;
    IF p_job ->> 'job_kind'='correction'
       AND EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_rows) AS row_item(value)
            WHERE pg_catalog.jsonb_typeof(row_item.value -> 'frame_index')='number'
              AND (row_item.value ->> 'frame_index')::bigint
                    =(p_job ->> 'correction_frame')::bigint
       ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
    END IF;
    v_plan := p_facts -> 'execution_plan';
    v_windows := v_plan -> 'windows';
    IF pg_catalog.jsonb_typeof(v_windows) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_windows) > 8192 THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','plan_not_frozen');
    END IF;
    v_index := 0;
    FOR v_window IN SELECT item.value
        FROM pg_catalog.jsonb_array_elements(v_windows) WITH ORDINALITY AS item(value,ordinality)
        ORDER BY item.ordinality
    LOOP
        v_phase := v_window ->> 'phase';
        v_branch := v_window ->> 'branch';
        v_reply := aap_jobs_v1.video_writer_authority(
            'read_window',p_proof || pg_catalog.jsonb_build_object(
                'window_ordinal',v_window -> 'ordinal'
            )
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
           OR v_reply ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok',false,
                'code',aap_labeling_v1._video_writer_authority_code(
                    v_reply ->> 'reason'
                )
            );
        END IF;
        v_checkpoint := v_reply -> 'checkpoint';
        IF v_checkpoint IS NULL OR v_checkpoint='null'::jsonb THEN
            IF NOT v_gap THEN
                v_first_missing := v_window;
                v_first_missing_attempt := v_reply -> 'attempt';
            END IF;
            v_gap := true;
        ELSE
            IF v_gap THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
            END IF;
            IF NOT aap_labeling_v1._video_writer_exact_keys(
                v_checkpoint,ARRAY['manifest','payload_json']
            ) OR pg_catalog.jsonb_typeof(v_checkpoint -> 'manifest') IS DISTINCT FROM 'object'
              OR pg_catalog.jsonb_typeof(v_checkpoint -> 'payload_json') IS DISTINCT FROM 'string' THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
            END IF;
            v_manifest := v_checkpoint -> 'manifest';
            BEGIN
                v_payload := (v_checkpoint ->> 'payload_json')::jsonb;
            EXCEPTION WHEN OTHERS THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
            END;
            IF v_manifest ->> 'phase' IS DISTINCT FROM v_phase
               OR v_payload ->> 'branch' IS DISTINCT FROM v_branch
               OR v_manifest ->> 'window_ordinal' IS DISTINCT FROM v_window ->> 'ordinal'
               OR v_manifest ->> 'window_digest' IS DISTINCT FROM v_window ->> 'window_digest'
               OR v_manifest ->> 'context_span_digest'
                    IS DISTINCT FROM v_window #>> '{context_span,span_digest}'
               OR v_payload ->> 'result_digest' IS DISTINCT FROM v_manifest ->> 'result_digest'
               OR pg_catalog.jsonb_typeof(v_payload -> 'frame_results')
                    IS DISTINCT FROM 'array' THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
            END IF;
            IF v_phase='track' THEN
                v_canonical := v_canonical || (v_payload -> 'frame_results');
            END IF;
        END IF;
        IF v_phase='track' THEN
            IF v_track_ordinal > 0
               AND v_window #>> '{context_span,mode}'='session'
               AND v_window #>> '{context_span,start_ordinal}'=(v_window ->> 'ordinal') THEN
                v_boundary := CASE WHEN v_branch='forward'
                    THEN v_window -> 'from_frame' ELSE v_window -> 'to_frame' END;
                v_boundaries := v_boundaries || pg_catalog.jsonb_build_array(v_boundary);
            END IF;
            v_track_ordinal := v_track_ordinal + 1;
        END IF;
        v_index := v_index + 1;
    END LOOP;
    IF p_requested_outcome='completed' THEN
        IF v_gap OR v_rows IS DISTINCT FROM v_canonical THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
        END IF;
    ELSE
        v_prefix_count := pg_catalog.jsonb_array_length(v_canonical);
        v_result_count := pg_catalog.jsonb_array_length(v_rows);
        IF v_result_count < v_prefix_count THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
        END IF;
        IF v_prefix_count > 0 AND NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_canonical) WITH ORDINALITY AS prefix(value,ordinality)
            JOIN pg_catalog.jsonb_array_elements(v_rows) WITH ORDINALITY AS supplied(value,ordinality)
              USING (ordinality)
            WHERE prefix.value IS DISTINCT FROM supplied.value
        ) THEN
            NULL;
        ELSIF v_prefix_count > 0 THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
        END IF;
        IF v_result_count > v_prefix_count THEN
            v_tail_branch := v_first_missing ->> 'branch';
            IF v_first_missing IS NULL
               OR v_first_missing ->> 'phase' IS DISTINCT FROM 'track'
               OR v_first_missing_attempt IS NULL
               OR v_first_missing_attempt='null'::jsonb
               OR v_first_missing_attempt ->> 'mode' IS DISTINCT FROM 'execute'
               OR v_first_missing_attempt ->> 'state' NOT IN ('started','capturing')
               OR (v_first_missing_attempt ->> 'generation')::bigint
                    IS DISTINCT FROM (p_proof ->> 'generation')::bigint
               OR (v_first_missing_attempt ->> 'window_ordinal')::integer
                    IS DISTINCT FROM (v_first_missing ->> 'ordinal')::integer THEN
                RETURN pg_catalog.jsonb_build_object('ok',false,'code','checkpoint_incomplete');
            END IF;
            v_start := (v_first_missing ->> 'from_frame')::bigint;
            v_end := (v_first_missing ->> 'to_frame')::bigint;
            v_prev_frame := NULL;
            FOR v_index IN v_prefix_count..v_result_count-1 LOOP
                v_window := v_rows -> v_index;
                IF pg_catalog.jsonb_typeof(v_window -> 'frame_index') <> 'number'
                   OR (v_window ->> 'frame_index') !~ '^(0|[1-9][0-9]*)$' THEN
                    RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
                END IF;
                v_frame := (v_window ->> 'frame_index')::bigint;
                IF v_frame < v_start OR v_frame > v_end
                   OR (p_job -> 'correction_frame' <> 'null'::jsonb
                       AND v_frame=(p_job ->> 'correction_frame')::bigint)
                   OR (v_prev_frame IS NOT NULL AND (
                       (v_tail_branch='forward' AND v_frame<v_prev_frame)
                       OR (v_tail_branch='backward' AND v_frame>v_prev_frame)
                   )) THEN
                    RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
                END IF;
                v_prev_frame := v_frame;
            END LOOP;
        END IF;
    END IF;
    IF p_stage ? 'tracker_context_boundaries' THEN
        IF v_boundaries='[]'::jsonb
           OR p_stage -> 'tracker_context_boundaries' IS DISTINCT FROM v_boundaries THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
        END IF;
    ELSIF p_requested_outcome='completed' AND v_boundaries<>'[]'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
    END IF;
    v_map := aap_labeling_v1._video_writer_source_map(
        CASE WHEN p_job -> 'annotation_id'='null'::jsonb THEN NULL
             ELSE (p_job ->> 'annotation_id')::uuid END,
        v_acceptance -> 'execution',v_rows
    );
    IF v_map IS NULL OR p_stage -> 'source_instance_map' IS DISTINCT FROM v_map THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','source_map_invalid');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(keys.object_key)
        ORDER BY keys.object_key COLLATE "C"),'[]'::jsonb)
    INTO v_keys
    FROM aap_labeling_v1._mask_gc_json_keys(p_stage) AS keys;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_keys) AS item(value)
        WHERE NOT aap_labeling_v1._mask_gc_key_valid(item.value #>> '{}')
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'code','candidate_invalid');
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ok',true,'source_map',v_map,'object_keys',v_keys
    );
EXCEPTION WHEN OTHERS THEN
    RETURN pg_catalog.jsonb_build_object('ok',false,'code','invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_writer_stage_validate(
    jsonb,jsonb,text,text,jsonb,jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.video_writer_step(
    p_action text,
    p_arguments text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_args jsonb;
    v_keys text[];
    v_execution jsonb;
    v_proof jsonb;
    v_authority jsonb;
    v_facts_reply jsonb;
    v_facts jsonb;
    v_job record;
    v_job_json jsonb;
    v_plan jsonb;
    v_expected jsonb;
    v_stage jsonb;
    v_stage_reply jsonb;
    v_mask_reply jsonb;
    v_receipt_row aap_labeling_v1.video_writer_receipts%ROWTYPE;
    v_receipt jsonb;
    v_data jsonb;
    v_code text;
    v_op uuid;
    v_job_id uuid;
    v_run_id uuid;
    v_worker_id uuid;
    v_generation bigint;
    v_lease_token uuid;
    v_digest text;
    v_requested_outcome text;
    v_effective_outcome text;
    v_error_message text;
    v_staged_text text;
    v_staged_sha256 text;
    v_staged_byte_size bigint;
    v_applied_stage boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_completed_at timestamptz;
    v_error_state text;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 135266304 THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    IF p_action='policy' THEN
        IF NOT aap_labeling_v1._video_writer_exact_keys(v_args,ARRAY[]::text[]) THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        RETURN aap_labeling_v1._video_writer_reply(pg_catalog.jsonb_build_object(
            'functions',pg_catalog.jsonb_build_array(
                'aap_labeling_v1.video_writer_step(text,text)'
            ),
            'max_request_bytes',68157440,
            'max_staged_bytes',67108864
        ));
    ELSIF p_action='read_publication' THEN
        v_keys := ARRAY['execution'];
    ELSIF p_action='status' THEN
        v_keys := ARRAY['operation_id','execution','request_digest'];
    ELSIF p_action='read_window' THEN
        v_keys := ARRAY['operation_id','execution','request_digest','window_ordinal'];
    ELSIF p_action='prepare' THEN
        v_keys := ARRAY[
            'operation_id','execution','outcome','request_digest',
            'expected_input','error_message'
        ];
    ELSIF p_action='lock_masks' THEN
        v_keys := ARRAY[
            'operation_id','execution','outcome','request_digest',
            'expected_input','error_message','object_keys'
        ];
    ELSIF p_action='apply' THEN
        v_keys := ARRAY[
            'operation_id','execution','outcome','request_digest',
            'expected_input','error_message','staged_result_json',
            'staged_sha256','staged_byte_size'
        ];
    ELSE
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    IF NOT aap_labeling_v1._video_writer_exact_keys(v_args,v_keys) THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    IF (p_action<>'read_publication' AND (
            pg_catalog.jsonb_typeof(v_args -> 'operation_id') IS DISTINCT FROM 'string'
            OR pg_catalog.jsonb_typeof(v_args -> 'request_digest') IS DISTINCT FROM 'string'
            OR NOT aap_labeling_v1._video_writer_digest_valid(v_args ->> 'request_digest')
       ))
       OR pg_catalog.jsonb_typeof(v_args -> 'execution') IS DISTINCT FROM 'object'
       OR NOT aap_labeling_v1._video_writer_exact_keys(
            v_args -> 'execution',
            ARRAY['job_id','run_id','worker_id','generation','lease_token']
       ) THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        v_op := (v_args ->> 'operation_id')::uuid;
        v_job_id := (v_args #>> '{execution,job_id}')::uuid;
        v_run_id := (v_args #>> '{execution,run_id}')::uuid;
        v_worker_id := (v_args #>> '{execution,worker_id}')::uuid;
        v_lease_token := (v_args #>> '{execution,lease_token}')::uuid;
        IF v_args #>> '{execution,generation}' !~ '^[1-9][0-9]*$'
           OR pg_catalog.jsonb_typeof(v_args #> '{execution,generation}') IS DISTINCT FROM 'number' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        v_generation := (v_args #>> '{execution,generation}')::bigint;
        IF (p_action<>'read_publication' AND v_op::text IS DISTINCT FROM v_args ->> 'operation_id')
           OR v_job_id::text IS DISTINCT FROM v_args #>> '{execution,job_id}'
           OR v_run_id::text IS DISTINCT FROM v_args #>> '{execution,run_id}'
           OR v_worker_id::text IS DISTINCT FROM v_args #>> '{execution,worker_id}'
           OR v_lease_token::text IS DISTINCT FROM v_args #>> '{execution,lease_token}'
           OR v_generation < 1 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END;
    v_digest := v_args ->> 'request_digest';
    v_proof := v_args -> 'execution';
    IF p_action='read_window' THEN
        IF pg_catalog.jsonb_typeof(v_args -> 'window_ordinal') IS DISTINCT FROM 'number'
           OR (v_args ->> 'window_ordinal') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_args ->> 'window_ordinal') > 4
           OR (v_args ->> 'window_ordinal')::integer NOT BETWEEN 0 AND 8191 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
    END IF;
    IF p_action IN ('prepare','lock_masks','apply') THEN
        v_requested_outcome := v_args ->> 'outcome';
        IF pg_catalog.jsonb_typeof(v_args -> 'outcome') IS DISTINCT FROM 'string'
           OR v_requested_outcome NOT IN ('completed','cancelled','failed')
           OR (v_args -> 'expected_input' IS DISTINCT FROM 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_args -> 'expected_input') IS DISTINCT FROM 'object')
           OR (v_args -> 'error_message' IS DISTINCT FROM 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_args -> 'error_message') IS DISTINCT FROM 'string') THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        IF v_requested_outcome='failed' THEN
            IF v_args -> 'error_message'='null'::jsonb
               OR pg_catalog.char_length(v_args ->> 'error_message') > 2000 THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
            END IF;
        ELSIF v_args -> 'error_message' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        v_expected := v_args -> 'expected_input';
        v_error_message := CASE WHEN v_args -> 'error_message'='null'::jsonb
                                THEN NULL ELSE v_args ->> 'error_message' END;
    END IF;
    IF p_action='lock_masks'
       AND pg_catalog.jsonb_typeof(v_args -> 'object_keys') IS DISTINCT FROM 'array' THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    END IF;
    IF p_action='apply' THEN
        IF (v_args -> 'staged_result_json' IS DISTINCT FROM 'null'::jsonb
            AND pg_catalog.jsonb_typeof(v_args -> 'staged_result_json') IS DISTINCT FROM 'string')
           OR (v_args -> 'staged_sha256' IS DISTINCT FROM 'null'::jsonb
               AND (pg_catalog.jsonb_typeof(v_args -> 'staged_sha256') IS DISTINCT FROM 'string'
                    OR NOT aap_labeling_v1._video_writer_digest_valid(
                        v_args ->> 'staged_sha256'
                    )))
           OR pg_catalog.jsonb_typeof(v_args -> 'staged_byte_size') IS DISTINCT FROM 'number'
           OR (v_args ->> 'staged_byte_size') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_args ->> 'staged_byte_size') > 10 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
        END IF;
        v_staged_byte_size := (v_args ->> 'staged_byte_size')::bigint;
        v_staged_text := CASE WHEN v_args -> 'staged_result_json'='null'::jsonb
                              THEN NULL ELSE v_args ->> 'staged_result_json' END;
        v_staged_sha256 := CASE WHEN v_args -> 'staged_sha256'='null'::jsonb
                                THEN NULL ELSE v_args ->> 'staged_sha256' END;
        IF v_staged_text IS NULL THEN
            IF v_staged_sha256 IS NOT NULL OR v_staged_byte_size<>0 THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
            END IF;
            v_stage := NULL;
        ELSE
            IF v_staged_byte_size>67108864 THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'staged_result_too_large');
            END IF;
            IF v_staged_byte_size=0
               OR pg_catalog.octet_length(v_staged_text)<>v_staged_byte_size
               OR pg_catalog.encode(public.digest(
                    pg_catalog.convert_to(v_staged_text,'UTF8'),'sha256'
                  ),'hex') IS DISTINCT FROM v_staged_sha256 THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
            END IF;
            BEGIN
                v_stage := v_staged_text::jsonb;
            EXCEPTION WHEN OTHERS THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
            END;
            IF pg_catalog.jsonb_typeof(v_stage) IS DISTINCT FROM 'object' THEN
                RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
            END IF;
        END IF;
        IF v_requested_outcome='failed' AND v_stage IS NOT NULL THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
        END IF;
    END IF;

    -- The Jobs check acquires Labeling Job -> Jobs execution in its fixed order.
    -- It must precede any receipt lookup so an old proof cannot replay a result.
    v_authority := aap_jobs_v1.video_writer_authority('inspect',v_proof);
    IF v_authority IS NULL
       OR v_authority ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
       OR v_authority ->> 'status' IS DISTINCT FROM 'ok' THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,
            aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason'));
    END IF;
    v_plan := v_authority -> 'plan';
    SELECT job.id,job.task_id,job.dataset_item_id,job.annotation_id,job.created_by,
           job.job_kind,job.from_frame,job.to_frame,job.direction,job.status,
           job.event_channel,job.cancel_requested_at,job.started_at,job.segment_id,
           job.correction_frame
    INTO v_job
    FROM public.video_tracker_jobs AS job
    WHERE job.id=v_job_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'job_missing');
    END IF;
    v_job_json := pg_catalog.jsonb_build_object(
        'job_id',v_job.id,'task_id',v_job.task_id,
        'dataset_item_id',v_job.dataset_item_id,'annotation_id',v_job.annotation_id,
        'created_by',v_job.created_by,'job_kind',v_job.job_kind,
        'from_frame',v_job.from_frame,'to_frame',v_job.to_frame,
        'direction',v_job.direction,'status',v_job.status,
        'event_channel',v_job.event_channel,
        'cancel_requested_at',v_job.cancel_requested_at,
        'started_at',v_job.started_at,'segment_id',v_job.segment_id,
        'correction_frame',v_job.correction_frame
    );
    IF p_action='read_window' THEN
        v_authority := aap_jobs_v1.video_writer_authority(
            'read_window',v_proof || pg_catalog.jsonb_build_object(
                'window_ordinal',(v_args ->> 'window_ordinal')::integer
            )
        );
        IF v_authority IS NULL
           OR v_authority ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
           OR v_authority ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,
                aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason'));
        END IF;
        IF NOT aap_labeling_v1._video_writer_exact_keys(
            v_authority - ARRAY['schema','status'],ARRAY['checkpoint','attempt']
        ) OR (v_authority -> 'checkpoint' IS DISTINCT FROM 'null'::jsonb
              AND NOT aap_labeling_v1._video_writer_exact_keys(
                  v_authority -> 'checkpoint',ARRAY['manifest','payload_json']
              )) OR (v_authority -> 'attempt' IS DISTINCT FROM 'null'::jsonb
              AND NOT aap_labeling_v1._video_writer_exact_keys(
                  v_authority -> 'attempt',ARRAY['generation','window_ordinal','mode','state']
              )) THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_response');
        END IF;
        v_data := aap_labeling_v1._video_writer_reply(
            v_authority - ARRAY['schema','status']
        );
        IF pg_catalog.octet_length(v_data::text)>135266304 THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'checkpoint_incomplete');
        END IF;
        RETURN v_data;
    END IF;
    IF p_action IN ('prepare','apply','status') THEN
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap:video-writer:' || v_op::text,0)
        ) THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'execution_busy');
        END IF;
    END IF;
    SELECT receipt.* INTO v_receipt_row
    FROM aap_labeling_v1.video_writer_receipts AS receipt
    WHERE (p_action='read_publication' AND receipt.job_id=v_job_id AND receipt.run_id=v_run_id)
       OR (p_action<>'read_publication' AND receipt.operation_id=v_op);
    IF FOUND THEN
        IF v_receipt_row.job_id IS DISTINCT FROM v_job_id
           OR v_receipt_row.run_id IS DISTINCT FROM v_run_id
           OR (p_action<>'read_publication' AND v_receipt_row.request_digest IS DISTINCT FROM v_digest) THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'operation_conflict');
        END IF;
        v_receipt := pg_catalog.jsonb_build_object(
            'operation_id',v_receipt_row.operation_id,
            'job_id',v_receipt_row.job_id,
            'run_id',v_receipt_row.run_id,
            'request_digest',v_receipt_row.request_digest,
            'outcome',v_receipt_row.outcome,
            'status',v_receipt_row.status,
            'completed_at',pg_catalog.to_char(
                v_receipt_row.completed_at AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            ),
            'staged_sha256',v_receipt_row.staged_sha256,
            'staged_byte_size',v_receipt_row.staged_byte_size
        );
        IF p_action='prepare' THEN
            RETURN aap_labeling_v1._video_writer_reply(
                pg_catalog.jsonb_build_object('receipt',v_receipt,'facts',NULL)
            );
        ELSIF p_action IN ('apply','status','read_publication') THEN
            RETURN aap_labeling_v1._video_writer_reply(
                pg_catalog.jsonb_build_object('receipt',v_receipt)
            );
        ELSE
            RETURN aap_labeling_v1._video_writer_reply(NULL,'operation_conflict');
        END IF;
    END IF;
    IF p_action IN ('status','read_publication') THEN
        RETURN aap_labeling_v1._video_writer_reply(
            pg_catalog.jsonb_build_object('receipt',NULL)
        );
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_labeling_v1.video_writer_receipts AS receipt
        WHERE receipt.job_id=v_job_id AND receipt.run_id=v_run_id
    ) THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'operation_conflict');
    END IF;
    IF v_requested_outcome<>'failed' AND v_job.job_kind='tracking'
       AND (v_job.status='cancelled' OR v_job.cancel_requested_at IS NOT NULL)
       AND v_expected='null'::jsonb AND v_plan<>'null'::jsonb THEN
        v_authority := aap_jobs_v1.video_writer_authority('has_track_results',v_proof);
        IF v_authority ->> 'status' IS DISTINCT FROM 'ok'
           OR pg_catalog.jsonb_typeof(v_authority -> 'has_track_results') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,
                aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason'));
        END IF;
        v_job_json := v_job_json || pg_catalog.jsonb_build_object(
            'has_track_results',v_authority -> 'has_track_results'
        );
    END IF;
    v_facts_reply := aap_labeling_v1._video_writer_facts(
        v_job_json,v_requested_outcome,v_expected,v_plan
    );
    IF v_facts_reply IS NULL
       OR v_facts_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-writer-data/v1'
       OR v_facts_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,
            aap_labeling_v1._video_writer_authority_code(
                v_facts_reply #>> '{error,code}'
            ));
    END IF;
    v_facts := v_facts_reply #> '{data,facts}';
    v_effective_outcome := v_facts ->> 'effective_outcome';
    IF p_action='prepare' THEN
        v_authority := aap_jobs_v1.video_writer_authority(
            CASE WHEN v_requested_outcome='failed' THEN 'guard_fail'
                WHEN v_effective_outcome='cancelled' THEN 'guard_cancel'
                ELSE 'guard_stage' END,
            v_proof
        );
        IF v_authority IS NULL
           OR v_authority ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
           OR v_authority ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,
                aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason'));
        END IF;
        RETURN aap_labeling_v1._video_writer_reply(pg_catalog.jsonb_build_object(
            'receipt',NULL,'facts',v_facts
        ));
    END IF;
    v_authority := aap_jobs_v1.video_writer_authority(
        CASE WHEN v_requested_outcome='failed' THEN 'guard_fail'
            WHEN v_effective_outcome='cancelled' THEN 'guard_cancel'
            ELSE 'guard_stage' END,
        v_proof
    );
    IF v_authority IS NULL
       OR v_authority ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
       OR v_authority ->> 'status' IS DISTINCT FROM 'ok' THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,
            aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason'));
    END IF;
    IF p_action='lock_masks' THEN
        IF v_effective_outcome NOT IN ('completed','cancelled')
           OR (v_facts -> 'acceptance') IS NULL
           OR v_facts -> 'acceptance'='null'::jsonb THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
        END IF;
        v_mask_reply := aap_labeling_v1._video_writer_mask_epochs(
            v_args -> 'object_keys'
        );
        IF v_mask_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='A0001',
                MESSAGE='writer_reject:' ||
                    aap_labeling_v1._video_writer_authority_code(v_mask_reply ->> 'code');
        END IF;
        RETURN aap_labeling_v1._video_writer_reply(pg_catalog.jsonb_build_object(
            'epochs',v_mask_reply -> 'epochs'
        ));
    END IF;

    IF v_requested_outcome='failed' THEN
        IF v_stage IS NOT NULL THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,'candidate_invalid');
        END IF;
    ELSE
        v_stage_reply := aap_labeling_v1._video_writer_stage_validate(
            v_proof,v_job_json,v_requested_outcome,v_effective_outcome,v_facts,v_stage
        );
        IF v_stage_reply IS NULL OR v_stage_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_labeling_v1._video_writer_reply(NULL,
                aap_labeling_v1._video_writer_authority_code(v_stage_reply ->> 'code'));
        END IF;
        IF v_stage IS NOT NULL THEN
            v_mask_reply := aap_labeling_v1._video_writer_mask_epochs(
                v_stage_reply -> 'object_keys'
            );
            IF v_mask_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE='A0001',
                    MESSAGE='writer_reject:' ||
                        aap_labeling_v1._video_writer_authority_code(v_mask_reply ->> 'code');
            END IF;
        END IF;
    END IF;
    IF v_effective_outcome='completed' THEN
        v_applied_stage := v_stage IS NOT NULL;
        UPDATE public.video_tracker_jobs AS job
        SET status='pending_review',
            staged_result=v_stage,
            completed_at=COALESCE(job.completed_at,v_now),
            updated_at=v_now
        WHERE job.id=v_job_id RETURNING job.completed_at INTO v_completed_at;
    ELSIF v_effective_outcome='cancelled' THEN
        v_applied_stage := v_stage IS NOT NULL
            AND v_job.job_kind='tracking';
        UPDATE public.video_tracker_jobs AS job
        SET status='cancelled',
            staged_result=CASE WHEN v_requested_outcome='failed' THEN job.staged_result
                               WHEN job.job_kind='correction' THEN NULL
                               WHEN v_stage IS NOT NULL THEN v_stage
                               ELSE job.staged_result END,
            completed_at=COALESCE(job.completed_at,v_now),
            updated_at=v_now
        WHERE job.id=v_job_id RETURNING job.completed_at INTO v_completed_at;
    ELSE
        UPDATE public.video_tracker_jobs AS job
        SET status='failed',staged_result=NULL,
            error_message=v_error_message,
            completed_at=COALESCE(job.completed_at,v_now),
            updated_at=v_now
        WHERE job.id=v_job_id RETURNING job.completed_at INTO v_completed_at;
    END IF;
    v_authority := aap_jobs_v1.video_writer_authority(
        CASE v_effective_outcome
            WHEN 'completed' THEN 'finish_completed'
            WHEN 'cancelled' THEN 'finish_cancelled'
            ELSE 'finish_failed' END,
        v_proof
    );
    IF v_authority IS NULL
       OR v_authority ->> 'schema' IS DISTINCT FROM 'aap-video-checkpoint-data/v1'
       OR v_authority ->> 'status' IS DISTINCT FROM 'ok' THEN
        RAISE EXCEPTION USING ERRCODE='A0001',
            MESSAGE='writer_reject:' ||
                aap_labeling_v1._video_writer_authority_code(v_authority ->> 'reason');
    END IF;
    INSERT INTO aap_labeling_v1.video_writer_receipts(
        operation_id,job_id,run_id,request_digest,outcome,status,
        completed_at,staged_sha256,staged_byte_size
    ) VALUES (
        v_op,v_job_id,v_run_id,v_digest,v_effective_outcome,
        CASE v_effective_outcome
            WHEN 'completed' THEN 'pending_review'
            WHEN 'cancelled' THEN 'cancelled'
            ELSE 'failed' END,
        v_completed_at,
        CASE WHEN v_applied_stage THEN v_staged_sha256 ELSE NULL END,
        CASE WHEN v_applied_stage THEN v_staged_byte_size ELSE 0 END
    ) RETURNING * INTO v_receipt_row;
    v_receipt := pg_catalog.jsonb_build_object(
        'operation_id',v_receipt_row.operation_id,
        'job_id',v_receipt_row.job_id,
        'run_id',v_receipt_row.run_id,
        'request_digest',v_receipt_row.request_digest,
        'outcome',v_receipt_row.outcome,
        'status',v_receipt_row.status,
        'completed_at',pg_catalog.to_char(
            v_receipt_row.completed_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'staged_sha256',v_receipt_row.staged_sha256,
        'staged_byte_size',v_receipt_row.staged_byte_size
    );
    RETURN aap_labeling_v1._video_writer_reply(
        pg_catalog.jsonb_build_object('receipt',v_receipt)
    );
EXCEPTION
    WHEN SQLSTATE 'A0001' THEN
        GET STACKED DIAGNOSTICS v_error_state = MESSAGE_TEXT;
        RETURN aap_labeling_v1._video_writer_reply(NULL,
            aap_labeling_v1._video_writer_authority_code(
                pg_catalog.substr(v_error_state,15)
            ));
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'execution_busy');
    WHEN unique_violation THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'operation_conflict');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'invalid_request');
    WHEN OTHERS THEN
        RETURN aap_labeling_v1._video_writer_reply(NULL,'storage_unavailable');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_writer_step(text,text)
    FROM PUBLIC;
-- aap-statement-break
