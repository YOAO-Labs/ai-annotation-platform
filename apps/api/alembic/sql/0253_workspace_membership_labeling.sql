-- Python str() of legacy JSON containers is part of participant attribution.
-- Unicode 15 non-printable ranges match the existing Python 3.12 data reader.
CREATE FUNCTION aap_labeling_v1._membership_python_repr(p_value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET extra_float_digits = 3
AS $function$
DECLARE
    v_nonprintable pg_catalog.int4multirange := '{[0,32),[127,161),[173,174),[888,890),[896,900),[907,908),[909,910),[930,931),[1328,1329),[1367,1369),[1419,1421),[1424,1425),[1480,1488),[1515,1519),[1525,1542),[1564,1565),[1757,1758),[1806,1808),[1867,1869),[1970,1984),[2043,2045),[2094,2096),[2111,2112),[2140,2142),[2143,2144),[2155,2160),[2191,2200),[2274,2275),[2436,2437),[2445,2447),[2449,2451),[2473,2474),[2481,2482),[2483,2486),[2490,2492),[2501,2503),[2505,2507),[2511,2519),[2520,2524),[2526,2527),[2532,2534),[2559,2561),[2564,2565),[2571,2575),[2577,2579),[2601,2602),[2609,2610),[2612,2613),[2615,2616),[2618,2620),[2621,2622),[2627,2631),[2633,2635),[2638,2641),[2642,2649),[2653,2654),[2655,2662),[2679,2689),[2692,2693),[2702,2703),[2706,2707),[2729,2730),[2737,2738),[2740,2741),[2746,2748),[2758,2759),[2762,2763),[2766,2768),[2769,2784),[2788,2790),[2802,2809),[2816,2817),[2820,2821),[2829,2831),[2833,2835),[2857,2858),[2865,2866),[2868,2869),[2874,2876),[2885,2887),[2889,2891),[2894,2901),[2904,2908),[2910,2911),[2916,2918),[2936,2946),[2948,2949),[2955,2958),[2961,2962),[2966,2969),[2971,2972),[2973,2974),[2976,2979),[2981,2984),[2987,2990),[3002,3006),[3011,3014),[3017,3018),[3022,3024),[3025,3031),[3032,3046),[3067,3072),[3085,3086),[3089,3090),[3113,3114),[3130,3132),[3141,3142),[3145,3146),[3150,3157),[3159,3160),[3163,3165),[3166,3168),[3172,3174),[3184,3191),[3213,3214),[3217,3218),[3241,3242),[3252,3253),[3258,3260),[3269,3270),[3273,3274),[3278,3285),[3287,3293),[3295,3296),[3300,3302),[3312,3313),[3316,3328),[3341,3342),[3345,3346),[3397,3398),[3401,3402),[3408,3412),[3428,3430),[3456,3457),[3460,3461),[3479,3482),[3506,3507),[3516,3517),[3518,3520),[3527,3530),[3531,3535),[3541,3542),[3543,3544),[3552,3558),[3568,3570),[3573,3585),[3643,3647),[3676,3713),[3715,3716),[3717,3718),[3723,3724),[3748,3749),[3750,3751),[3774,3776),[3781,3782),[3783,3784),[3791,3792),[3802,3804),[3808,3840),[3912,3913),[3949,3953),[3992,3993),[4029,4030),[4045,4046),[4059,4096),[4294,4295),[4296,4301),[4302,4304),[4681,4682),[4686,4688),[4695,4696),[4697,4698),[4702,4704),[4745,4746),[4750,4752),[4785,4786),[4790,4792),[4799,4800),[4801,4802),[4806,4808),[4823,4824),[4881,4882),[4886,4888),[4955,4957),[4989,4992),[5018,5024),[5110,5112),[5118,5120),[5760,5761),[5789,5792),[5881,5888),[5910,5919),[5943,5952),[5972,5984),[5997,5998),[6001,6002),[6004,6016),[6110,6112),[6122,6128),[6138,6144),[6158,6159),[6170,6176),[6265,6272),[6315,6320),[6390,6400),[6431,6432),[6444,6448),[6460,6464),[6465,6468),[6510,6512),[6517,6528),[6572,6576),[6602,6608),[6619,6622),[6684,6686),[6751,6752),[6781,6783),[6794,6800),[6810,6816),[6830,6832),[6863,6912),[6989,6992),[7039,7040),[7156,7164),[7224,7227),[7242,7245),[7305,7312),[7355,7357),[7368,7376),[7419,7424),[7958,7960),[7966,7968),[8006,8008),[8014,8016),[8024,8025),[8026,8027),[8028,8029),[8030,8031),[8062,8064),[8117,8118),[8133,8134),[8148,8150),[8156,8157),[8176,8178),[8181,8182),[8191,8208),[8232,8240),[8287,8304),[8306,8308),[8335,8336),[8349,8352),[8385,8400),[8433,8448),[8588,8592),[9255,9280),[9291,9312),[11124,11126),[11158,11159),[11508,11513),[11558,11559),[11560,11565),[11566,11568),[11624,11631),[11633,11647),[11671,11680),[11687,11688),[11695,11696),[11703,11704),[11711,11712),[11719,11720),[11727,11728),[11735,11736),[11743,11744),[11870,11904),[11930,11931),[12020,12032),[12246,12272),[12284,12289),[12352,12353),[12439,12441),[12544,12549),[12592,12593),[12687,12688),[12772,12784),[12831,12832),[42125,42128),[42183,42192),[42540,42560),[42744,42752),[42955,42960),[42962,42963),[42964,42965),[42970,42994),[43053,43056),[43066,43072),[43128,43136),[43206,43214),[43226,43232),[43348,43359),[43389,43392),[43470,43471),[43482,43486),[43519,43520),[43575,43584),[43598,43600),[43610,43612),[43715,43739),[43767,43777),[43783,43785),[43791,43793),[43799,43808),[43815,43816),[43823,43824),[43884,43888),[44014,44016),[44026,44032),[55204,55216),[55239,55243),[55292,63744),[64110,64112),[64218,64256),[64263,64275),[64280,64285),[64311,64312),[64317,64318),[64319,64320),[64322,64323),[64325,64326),[64451,64467),[64912,64914),[64968,64975),[64976,65008),[65050,65056),[65107,65108),[65127,65128),[65132,65136),[65141,65142),[65277,65281),[65471,65474),[65480,65482),[65488,65490),[65496,65498),[65501,65504),[65511,65512),[65519,65532),[65534,65536),[65548,65549),[65575,65576),[65595,65596),[65598,65599),[65614,65616),[65630,65664),[65787,65792),[65795,65799),[65844,65847),[65935,65936),[65949,65952),[65953,66000),[66046,66176),[66205,66208),[66257,66272),[66300,66304),[66340,66349),[66379,66384),[66427,66432),[66462,66463),[66500,66504),[66518,66560),[66718,66720),[66730,66736),[66772,66776),[66812,66816),[66856,66864),[66916,66927),[66939,66940),[66955,66956),[66963,66964),[66966,66967),[66978,66979),[66994,66995),[67002,67003),[67005,67072),[67383,67392),[67414,67424),[67432,67456),[67462,67463),[67505,67506),[67515,67584),[67590,67592),[67593,67594),[67638,67639),[67641,67644),[67645,67647),[67670,67671),[67743,67751),[67760,67808),[67827,67828),[67830,67835),[67868,67871),[67898,67903),[67904,67968),[68024,68028),[68048,68050),[68100,68101),[68103,68108),[68116,68117),[68120,68121),[68150,68152),[68155,68159),[68169,68176),[68185,68192),[68256,68288),[68327,68331),[68343,68352),[68406,68409),[68438,68440),[68467,68472),[68498,68505),[68509,68521),[68528,68608),[68681,68736),[68787,68800),[68851,68858),[68904,68912),[68922,69216),[69247,69248),[69290,69291),[69294,69296),[69298,69373),[69416,69424),[69466,69488),[69514,69552),[69580,69600),[69623,69632),[69710,69714),[69750,69759),[69821,69822),[69827,69840),[69865,69872),[69882,69888),[69941,69942),[69960,69968),[70007,70016),[70112,70113),[70133,70144),[70162,70163),[70210,70272),[70279,70280),[70281,70282),[70286,70287),[70302,70303),[70314,70320),[70379,70384),[70394,70400),[70404,70405),[70413,70415),[70417,70419),[70441,70442),[70449,70450),[70452,70453),[70458,70459),[70469,70471),[70473,70475),[70478,70480),[70481,70487),[70488,70493),[70500,70502),[70509,70512),[70517,70656),[70748,70749),[70754,70784),[70856,70864),[70874,71040),[71094,71096),[71134,71168),[71237,71248),[71258,71264),[71277,71296),[71354,71360),[71370,71424),[71451,71453),[71468,71472),[71495,71680),[71740,71840),[71923,71935),[71943,71945),[71946,71948),[71956,71957),[71959,71960),[71990,71991),[71993,71995),[72007,72016),[72026,72096),[72104,72106),[72152,72154),[72165,72192),[72264,72272),[72355,72368),[72441,72448),[72458,72704),[72713,72714),[72759,72760),[72774,72784),[72813,72816),[72848,72850),[72872,72873),[72887,72960),[72967,72968),[72970,72971),[73015,73018),[73019,73020),[73022,73023),[73032,73040),[73050,73056),[73062,73063),[73065,73066),[73103,73104),[73106,73107),[73113,73120),[73130,73440),[73465,73472),[73489,73490),[73531,73534),[73562,73648),[73649,73664),[73714,73727),[74650,74752),[74863,74864),[74869,74880),[75076,77712),[77811,77824),[78896,78912),[78934,82944),[83527,92160),[92729,92736),[92767,92768),[92778,92782),[92863,92864),[92874,92880),[92910,92912),[92918,92928),[92998,93008),[93018,93019),[93026,93027),[93048,93053),[93072,93760),[93851,93952),[94027,94031),[94088,94095),[94112,94176),[94181,94192),[94194,94208),[100344,100352),[101590,101632),[101641,110576),[110580,110581),[110588,110589),[110591,110592),[110883,110898),[110899,110928),[110931,110933),[110934,110948),[110952,110960),[111356,113664),[113771,113776),[113789,113792),[113801,113808),[113818,113820),[113824,118528),[118574,118576),[118599,118608),[118724,118784),[119030,119040),[119079,119081),[119155,119163),[119275,119296),[119366,119488),[119508,119520),[119540,119552),[119639,119648),[119673,119808),[119893,119894),[119965,119966),[119968,119970),[119971,119973),[119975,119977),[119981,119982),[119994,119995),[119996,119997),[120004,120005),[120070,120071),[120075,120077),[120085,120086),[120093,120094),[120122,120123),[120127,120128),[120133,120134),[120135,120138),[120145,120146),[120486,120488),[120780,120782),[121484,121499),[121504,121505),[121520,122624),[122655,122661),[122667,122880),[122887,122888),[122905,122907),[122914,122915),[122917,122918),[122923,122928),[122990,123023),[123024,123136),[123181,123184),[123198,123200),[123210,123214),[123216,123536),[123567,123584),[123642,123647),[123648,124112),[124154,124896),[124903,124904),[124908,124909),[124911,124912),[124927,124928),[125125,125127),[125143,125184),[125260,125264),[125274,125278),[125280,126065),[126133,126209),[126270,126464),[126468,126469),[126496,126497),[126499,126500),[126501,126503),[126504,126505),[126515,126516),[126520,126521),[126522,126523),[126524,126530),[126531,126535),[126536,126537),[126538,126539),[126540,126541),[126544,126545),[126547,126548),[126549,126551),[126552,126553),[126554,126555),[126556,126557),[126558,126559),[126560,126561),[126563,126564),[126565,126567),[126571,126572),[126579,126580),[126584,126585),[126589,126590),[126591,126592),[126602,126603),[126620,126625),[126628,126629),[126634,126635),[126652,126704),[126706,126976),[127020,127024),[127124,127136),[127151,127153),[127168,127169),[127184,127185),[127222,127232),[127406,127462),[127491,127504),[127548,127552),[127561,127568),[127570,127584),[127590,127744),[128728,128732),[128749,128752),[128765,128768),[128887,128891),[128986,128992),[129004,129008),[129009,129024),[129036,129040),[129096,129104),[129114,129120),[129160,129168),[129198,129200),[129202,129280),[129620,129632),[129646,129648),[129661,129664),[129673,129680),[129726,129727),[129734,129742),[129756,129760),[129769,129776),[129785,129792),[129939,129940),[129995,130032),[130042,131072),[173792,173824),[177978,177984),[178206,178208),[183970,183984),[191457,194560),[195102,196608),[201547,201552),[205744,917760),[918000,1114112)}'::pg_catalog.int4multirange;
    v_kind text := pg_catalog.jsonb_typeof(p_value);
    v_source text;
    v_result text := '';
    v_quote text;
    v_character text;
    v_code integer;
    v_index integer;
    v_entry record;
    v_item jsonb;
    v_negative text := '';
    v_digits text;
    v_full_digits text;
    v_exponent integer := 0;
    v_decimal integer;
BEGIN
    IF p_value IS NULL OR v_kind='null' THEN RETURN 'None'; END IF;
    IF v_kind='boolean' THEN
        RETURN CASE WHEN p_value='true'::jsonb THEN 'True' ELSE 'False' END;
    ELSIF v_kind='number' THEN
        v_source := p_value::text;
        IF pg_catalog.strpos(v_source,'.')=0
           AND pg_catalog.strpos(pg_catalog.lower(v_source),'e')=0 THEN
            RETURN v_source;
        END IF;
        BEGIN
            v_source := (v_source::double precision)::text;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RETURN CASE WHEN p_value::numeric < 0 THEN '-inf' ELSE 'inf' END;
        END;
        IF pg_catalog.left(v_source,1)='-' THEN
            v_negative := '-'; v_source := pg_catalog.substr(v_source,2);
        END IF;
        IF pg_catalog.strpos(v_source,'e')>0 THEN
            v_exponent := pg_catalog.split_part(v_source,'e',2)::integer;
            v_source := pg_catalog.split_part(v_source,'e',1);
        END IF;
        v_full_digits := pg_catalog.replace(v_source,'.','');
        v_decimal := CASE WHEN pg_catalog.strpos(v_source,'.')>0
            THEN pg_catalog.strpos(v_source,'.')-1
            ELSE pg_catalog.char_length(v_source) END;
        v_digits := pg_catalog.ltrim(v_full_digits,'0');
        IF v_digits='' THEN RETURN v_negative || '0.0'; END IF;
        v_exponent := v_exponent + v_decimal
            - (pg_catalog.char_length(v_full_digits)-pg_catalog.char_length(v_digits))-1;
        v_digits := pg_catalog.rtrim(v_digits,'0');
        IF v_exponent < -4 OR v_exponent >= 16 THEN
            RETURN v_negative || pg_catalog.left(v_digits,1)
                || CASE WHEN pg_catalog.char_length(v_digits)>1
                    THEN '.' || pg_catalog.substr(v_digits,2) ELSE '' END
                || 'e' || CASE WHEN v_exponent<0 THEN '-' ELSE '+' END
                || CASE WHEN pg_catalog.abs(v_exponent)<10 THEN '0' ELSE '' END
                || pg_catalog.abs(v_exponent)::text;
        END IF;
        v_decimal := v_exponent+1;
        IF v_decimal<=0 THEN
            RETURN v_negative || '0.' || pg_catalog.repeat('0',-v_decimal) || v_digits;
        ELSIF v_decimal>=pg_catalog.char_length(v_digits) THEN
            RETURN v_negative || v_digits
                || pg_catalog.repeat('0',v_decimal-pg_catalog.char_length(v_digits)) || '.0';
        END IF;
        RETURN v_negative || pg_catalog.left(v_digits,v_decimal)
            || '.' || pg_catalog.substr(v_digits,v_decimal+1);
    ELSIF v_kind='string' THEN
        v_source := p_value #>> '{}';
        v_quote := CASE WHEN pg_catalog.strpos(v_source,pg_catalog.chr(39))>0
            AND pg_catalog.strpos(v_source,pg_catalog.chr(34))=0
            THEN pg_catalog.chr(34) ELSE pg_catalog.chr(39) END;
        FOR v_index IN 1..pg_catalog.char_length(v_source) LOOP
            v_character := pg_catalog.substr(v_source,v_index,1);
            v_code := pg_catalog.ascii(v_character);
            IF v_character=v_quote OR v_code=92 THEN
                v_result := v_result || pg_catalog.chr(92) || v_character;
            ELSIF v_code IN (9,10,13) THEN
                v_result := v_result || pg_catalog.chr(92)
                    || CASE v_code WHEN 9 THEN 't' WHEN 10 THEN 'n' ELSE 'r' END;
            ELSIF v_code <@ v_nonprintable THEN
                v_result := v_result || pg_catalog.chr(92)
                    || CASE WHEN v_code<256 THEN 'x' WHEN v_code<65536 THEN 'u' ELSE 'U' END
                    || pg_catalog.lpad(pg_catalog.to_hex(v_code),
                        CASE WHEN v_code<256 THEN 2 WHEN v_code<65536 THEN 4 ELSE 8 END,'0');
            ELSE
                v_result := v_result || v_character;
            END IF;
        END LOOP;
        RETURN v_quote || v_result || v_quote;
    ELSIF v_kind='array' THEN
        FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_value)
            WITH ORDINALITY AS entry(value,ordinal) ORDER BY entry.ordinal LOOP
            v_result := v_result || CASE WHEN v_result='' THEN '' ELSE ', ' END
                || aap_labeling_v1._membership_python_repr(v_item);
        END LOOP;
        RETURN '[' || v_result || ']';
    ELSIF v_kind='object' THEN
        FOR v_entry IN SELECT entry.key,entry.value FROM pg_catalog.jsonb_each(p_value) AS entry
            ORDER BY pg_catalog.octet_length(entry.key),entry.key COLLATE "C" LOOP
            v_result := v_result || CASE WHEN v_result='' THEN '' ELSE ', ' END
                || aap_labeling_v1._membership_python_repr(pg_catalog.to_jsonb(v_entry.key))
                || ': ' || aap_labeling_v1._membership_python_repr(v_entry.value);
        END LOOP;
        RETURN '{' || v_result || '}';
    END IF;
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid legacy participant value';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._membership_python_repr(jsonb) FROM PUBLIC;
-- aap-statement-break

-- Labeling-owned task, batch and task-lock facts and effects for Workspace
-- project-member role changes. Workspace retains the surrounding transaction.
CREATE FUNCTION aap_labeling_v1.read_membership_work(
    p_project_id uuid,
    p_user_id uuid,
    p_annotation_receiver_id uuid,
    p_review_receiver_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_unfinished_review CONSTANT text[] := ARRAY['review']::text[];
    v_unfinished_work CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected', 'review']::text[];
    v_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_unassigned_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_lock_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_claim_task_ids uuid[] := ARRAY[]::uuid[];
    v_batch_annotator_ids uuid[] := ARRAY[]::uuid[];
    v_batch_reviewer_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_affected_task_ids uuid[] := ARRAY[]::uuid[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_receiver_conflict_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_unknown_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_annotator_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_receiver_contributor_task_ids uuid[] := ARRAY[]::uuid[];
    v_tasks jsonb := '[]'::jsonb;
    v_batches jsonb := '[]'::jsonb;
BEGIN
    IF p_project_id IS NULL OR p_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_annotation_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_annotation)
      AND COALESCE(task.assignee_id, batch.annotator_id) = p_user_id;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_review_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_unassigned_review_task_ids
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND COALESCE(task.reviewer_id, batch.reviewer_id) IS NULL;

    SELECT COALESCE(
        pg_catalog.array_agg(task_lock.task_id ORDER BY task_lock.task_id),
        ARRAY[]::uuid[]
    )
    INTO v_lock_task_ids
    FROM public.task_locks AS task_lock
    JOIN public.tasks AS task ON task.id = task_lock.task_id
    WHERE task.project_id = p_project_id
      AND task_lock.user_id = p_user_id
      AND task_lock.expire_at > pg_catalog.statement_timestamp();

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_review_claim_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_review)
      AND task.reviewer_id = p_user_id
      AND task.reviewer_claimed_at IS NOT NULL;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_annotator_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.annotator_id = p_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_annotation)
            AND (
                task.assignee_id IS NULL
                OR (
                    task.assignee_id = p_user_id
                    AND task.assignee_is_override IS FALSE
                )
            )
      );

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_reviewer_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.reviewer_id = p_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_work)
            AND (
                task.reviewer_id IS NULL
                OR (
                    task.reviewer_id = p_user_id
                    AND task.reviewer_is_override IS FALSE
                )
            )
      );

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_materialization_annotation_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_annotator_ids)
      AND task.assignee_id IS NULL
      AND task.status <> ALL(v_unfinished_annotation);

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
    )
    INTO v_materialization_review_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_reviewer_ids)
      AND task.reviewer_id IS NULL
      AND task.status = 'completed';

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT requested.task_id ORDER BY requested.task_id),
        ARRAY[]::uuid[]
    )
    INTO v_affected_task_ids
    FROM pg_catalog.unnest(
        v_annotation_task_ids
        || v_review_task_ids
        || v_unassigned_review_task_ids
        || v_lock_task_ids
        || v_review_claim_task_ids
        || v_materialization_annotation_task_ids
        || v_materialization_review_task_ids
    ) AS requested(task_id);

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT requested.batch_id ORDER BY requested.batch_id),
        ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM pg_catalog.unnest(v_batch_annotator_ids || v_batch_reviewer_ids)
        AS requested(batch_id);

    IF p_annotation_receiver_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_annotation_receiver_conflict_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_annotation)
          AND COALESCE(task.assignee_id, batch.annotator_id) = p_user_id
          AND COALESCE(task.reviewer_id, batch.reviewer_id)
                = p_annotation_receiver_id;
    END IF;

    IF p_review_receiver_id IS NOT NULL THEN
        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_unknown_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND task.review_submitter_id IS DISTINCT FROM p_review_receiver_id
          AND COALESCE(task.annotation_contributor_ids, 'null'::jsonb)
                = 'null'::jsonb;

        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_annotator_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND COALESCE(task.assignee_id, batch.annotator_id)
                = p_review_receiver_id;

        SELECT COALESCE(
            pg_catalog.array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[]
        )
        INTO v_review_receiver_contributor_task_ids
        FROM public.tasks AS task
        LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
        WHERE task.project_id = p_project_id
          AND task.status = ANY(v_unfinished_work)
          AND COALESCE(task.reviewer_id, batch.reviewer_id) = p_user_id
          AND (
              task.review_submitter_id = p_review_receiver_id
              OR (
                  COALESCE(task.annotation_contributor_ids, 'null'::jsonb)
                        <> 'null'::jsonb
                  AND (
                      pg_catalog.jsonb_exists(
                          task.annotation_contributor_ids,
                          p_review_receiver_id::text
                      )
                      OR (
                          COALESCE(task.review_contributor_ids, 'null'::jsonb)
                                <> 'null'::jsonb
                          AND pg_catalog.jsonb_exists(
                              task.review_contributor_ids,
                              p_review_receiver_id::text
                          )
                      )
                  )
              )
          );
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', task.id,
                'status', task.status,
                'version', task.version,
                'batch_id', task.batch_id,
                'assignee_id', task.assignee_id,
                'assignee_is_override', task.assignee_is_override,
                'reviewer_id', task.reviewer_id,
                'reviewer_is_override', task.reviewer_is_override,
                'reviewer_claimed', task.reviewer_claimed_at IS NOT NULL,
                'review_round_id', task.review_round_id,
                'review_submitter_id', task.review_submitter_id,
                'annotation_contributor_ids', task.annotation_contributor_ids,
                'review_contributor_ids', task.review_contributor_ids,
                'batch_annotator_id', batch.annotator_id,
                'batch_reviewer_id', batch.reviewer_id
            ) ORDER BY task.id
        ),
        '[]'::jsonb
    )
    INTO v_tasks
    FROM public.tasks AS task
    LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
    WHERE task.project_id = p_project_id
      AND task.id = ANY(v_affected_task_ids);

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', batch.id,
                'status', batch.status,
                'annotator_id', batch.annotator_id,
                'reviewer_id', batch.reviewer_id,
                'assigned_user_ids', batch.assigned_user_ids,
                'admin_locked', batch.admin_locked
            ) ORDER BY batch.id
        ),
        '[]'::jsonb
    )
    INTO v_batches
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.id = ANY(v_batch_ids);

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-membership-work/v1',
        'project_id', p_project_id,
        'user_id', p_user_id,
        'annotation_task_ids', v_annotation_task_ids,
        'review_task_ids', v_review_task_ids,
        'unassigned_review_task_ids', v_unassigned_review_task_ids,
        'lock_task_ids', v_lock_task_ids,
        'review_claim_task_ids', v_review_claim_task_ids,
        'materialization_annotation_task_ids', v_materialization_annotation_task_ids,
        'materialization_review_task_ids', v_materialization_review_task_ids,
        'batch_annotator_ids', v_batch_annotator_ids,
        'batch_reviewer_ids', v_batch_reviewer_ids,
        'tasks', v_tasks,
        'batches', v_batches,
        'annotation_receiver_conflict_task_ids', v_annotation_receiver_conflict_task_ids,
        'review_receiver_unknown_task_ids', v_review_receiver_unknown_task_ids,
        'review_receiver_annotator_task_ids', v_review_receiver_annotator_task_ids,
        'review_receiver_contributor_task_ids', v_review_receiver_contributor_task_ids
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Acquire only the sorted resources selected by the old project-member workflow.
-- The caller rolls back the whole transaction after any typed contention result.
CREATE FUNCTION aap_labeling_v1.lock_membership_work(
    p_project_id uuid,
    p_user_id uuid,
    p_target_role text,
    p_is_removal boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_work jsonb;
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_lock_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_annotation_task_ids uuid[] := ARRAY[]::uuid[];
    v_materialization_review_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_advisory_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_id uuid;
BEGIN
    IF p_project_id IS NULL
       OR p_user_id IS NULL
       OR p_target_role IS NULL
       OR p_target_role NOT IN ('annotator', 'reviewer', 'viewer')
       OR p_is_removal IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_work := aap_labeling_v1.read_membership_work(
        p_project_id, p_user_id, NULL, NULL
    );
    IF v_work ->> 'schema' IS DISTINCT FROM 'aap-labeling-membership-work/v1' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid membership work facts';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM (
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_annotator_ids'
        ) AS entry(value)
        UNION
        SELECT entry.value::uuid AS id
        FROM pg_catalog.jsonb_array_elements_text(
            v_work -> 'batch_reviewer_ids'
        ) AS entry(value)
    ) AS ids;

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_lock_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'lock_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_annotation_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_annotation_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_materialization_review_task_ids
    FROM pg_catalog.jsonb_array_elements_text(
        v_work -> 'materialization_review_task_ids'
    ) AS entry(value);

    SELECT COALESCE(
        pg_catalog.array_agg(DISTINCT ids.id ORDER BY ids.id), ARRAY[]::uuid[]
    )
    INTO v_advisory_task_ids
    FROM pg_catalog.unnest(
        v_annotation_task_ids || v_review_task_ids || v_lock_task_ids
    ) AS ids(id);

    v_task_ids := v_advisory_task_ids;
    IF NOT p_is_removal THEN
        IF p_target_role <> 'annotator' THEN
            v_task_ids := v_task_ids || v_materialization_annotation_task_ids;
        END IF;
        IF p_target_role <> 'reviewer' THEN
            v_task_ids := v_task_ids || v_materialization_review_task_ids;
        END IF;
    END IF;

    BEGIN
        PERFORM batch.id
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '批次资源正在变更，请刷新后重试'
                )
            );
    END;

    BEGIN
        PERFORM task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
          AND task.id = ANY(v_task_ids)
        ORDER BY task.id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务资源正在变更，请刷新后重试'
                )
            );
    END;

    FOREACH v_task_id IN ARRAY v_advisory_task_ids LOOP
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap:task-edit-lock:' || v_task_id::text,
                0
            )
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务正在编辑，请刷新后重试'
                )
            );
        END IF;
    END LOOP;

    BEGIN
        PERFORM task_lock.id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task.project_id = p_project_id
          AND task_lock.user_id = p_user_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
        ORDER BY task_lock.task_id, task_lock.user_id
        FOR UPDATE OF task_lock NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR deadlock_detected OR serialization_failure THEN
            RETURN pg_catalog.jsonb_build_object(
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'reason', 'resource_busy',
                    'detail', '任务锁正在变更，请刷新后重试'
                )
            );
    END;

    RETURN pg_catalog.jsonb_build_object('ok', true);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_membership_work(uuid, uuid, text, boolean)
    FROM PUBLIC;
-- aap-statement-break

-- Mirror Python's BatchService._replace_assigned_user_id for legacy JSON values.
-- Batch assignment arrays are normally UUID strings; legacy iterable values are
-- still stringified with Python-style scalar and simple container representations.
CREATE FUNCTION aap_labeling_v1.handoff_membership_annotation(
    p_project_id uuid,
    p_from_user_id uuid,
    p_to_user_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_batch record;
    v_iterable jsonb;
    v_item jsonb;
    v_string text;
    v_updated jsonb;
    v_replaced boolean;
BEGIN
    IF p_project_id IS NULL OR p_from_user_id IS NULL OR p_to_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.annotator_id = p_from_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_annotation)
            AND (
                task.assignee_id IS NULL
                OR (
                    task.assignee_id = p_from_user_id
                    AND task.assignee_is_override IS FALSE
                )
            )
      );

    UPDATE public.tasks AS task
    SET assignee_id = p_to_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.status = ANY(v_unfinished_annotation)
      AND task.id IN (
          SELECT candidate.id
          FROM public.tasks AS candidate
          LEFT JOIN public.task_batches AS batch ON batch.id = candidate.batch_id
          WHERE candidate.project_id = p_project_id
            AND candidate.status = ANY(v_unfinished_annotation)
            AND COALESCE(candidate.assignee_id, batch.annotator_id)
                = p_from_user_id
      );

    -- Freeze historical NULL inheritance before changing the batch default.
    UPDATE public.tasks AS task
    SET assignee_id = p_from_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_ids)
      AND task.assignee_id IS NULL
      AND task.status <> ALL(v_unfinished_annotation);

    FOR v_batch IN
        SELECT batch.id, batch.assigned_user_ids
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
    LOOP
        IF v_batch.assigned_user_ids IS NULL
           OR v_batch.assigned_user_ids = 'null'::jsonb THEN
            v_iterable := '[]'::jsonb;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'array' THEN
            v_iterable := v_batch.assigned_user_ids;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'object' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(pg_catalog.to_jsonb(entry.key)
                    ORDER BY pg_catalog.octet_length(entry.key), entry.key COLLATE "C"),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.jsonb_object_keys(v_batch.assigned_user_ids)
                AS entry(key);
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'string' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.to_jsonb(pg_catalog.substr(
                        source_text.assigned_text, positions.character_number, 1
                    )) ORDER BY positions.character_number
                ),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.generate_series(
                1,
                pg_catalog.char_length(v_batch.assigned_user_ids #>> '{}')
            ) AS positions(character_number)
            CROSS JOIN LATERAL (
                SELECT v_batch.assigned_user_ids #>> '{}' AS assigned_text
            ) AS source_text;
        ELSE
            RAISE EXCEPTION 'legacy assigned_user_ids is not iterable';
        END IF;

        v_updated := '[]'::jsonb;
        v_replaced := false;
        FOR v_item IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_iterable) AS element(value)
        LOOP
            CASE pg_catalog.jsonb_typeof(v_item)
                WHEN 'string' THEN v_string := v_item #>> '{}';
                WHEN 'null' THEN v_string := 'None';
                WHEN 'boolean' THEN
                    v_string := CASE WHEN v_item = 'true'::jsonb
                        THEN 'True' ELSE 'False' END;
                WHEN 'number' THEN v_string := aap_labeling_v1._membership_python_repr(v_item);
                WHEN 'object', 'array' THEN
                    v_string := aap_labeling_v1._membership_python_repr(v_item);
                ELSE
                    RAISE EXCEPTION 'unsupported assigned_user_ids value';
            END CASE;

            IF v_string = p_from_user_id::text THEN
                v_string := p_to_user_id::text;
                v_replaced := true;
            END IF;
            v_updated := v_updated || pg_catalog.jsonb_build_array(v_string);
        END LOOP;

        IF NOT v_replaced THEN
            v_updated := v_updated || pg_catalog.jsonb_build_array(p_to_user_id::text);
        END IF;

        UPDATE public.task_batches AS batch
        SET annotator_id = p_to_user_id,
            assigned_user_ids = v_updated,
            updated_at = pg_catalog.now()
        WHERE batch.id = v_batch.id
          AND batch.project_id = p_project_id;
    END LOOP;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.handoff_membership_reviewer(
    p_project_id uuid,
    p_from_user_id uuid,
    p_to_user_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_unfinished_annotation CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected']::text[];
    v_unfinished_work CONSTANT text[] :=
        ARRAY['pending', 'in_progress', 'rejected', 'review']::text[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_batch record;
    v_iterable jsonb;
    v_item jsonb;
    v_string text;
    v_updated jsonb;
    v_replaced boolean;
BEGIN
    IF p_project_id IS NULL OR p_from_user_id IS NULL OR p_to_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id), ARRAY[]::uuid[]
    )
    INTO v_batch_ids
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.reviewer_id = p_from_user_id
      AND EXISTS (
          SELECT 1
          FROM public.tasks AS task
          WHERE task.project_id = p_project_id
            AND task.batch_id = batch.id
            AND task.status = ANY(v_unfinished_work)
            AND (
                task.reviewer_id IS NULL
                OR (
                    task.reviewer_id = p_from_user_id
                    AND task.reviewer_is_override IS FALSE
                )
            )
      );

    UPDATE public.tasks AS task
    SET reviewer_id = p_to_user_id,
        reviewer_claimed_at = NULL,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.status = 'review'
      AND task.id IN (
          SELECT candidate.id
          FROM public.tasks AS candidate
          LEFT JOIN public.task_batches AS batch ON batch.id = candidate.batch_id
          WHERE candidate.project_id = p_project_id
            AND candidate.status = 'review'
            AND COALESCE(candidate.reviewer_id, batch.reviewer_id)
                = p_from_user_id
      );

    -- Preserve the old reviewer on completed, NULL-inheriting task history.
    UPDATE public.tasks AS task
    SET reviewer_id = p_from_user_id,
        updated_at = pg_catalog.now()
    WHERE task.project_id = p_project_id
      AND task.batch_id = ANY(v_batch_ids)
      AND task.reviewer_id IS NULL
      AND task.status = 'completed';

    FOR v_batch IN
        SELECT batch.id, batch.assigned_user_ids
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
          AND batch.id = ANY(v_batch_ids)
        ORDER BY batch.id
    LOOP
        IF v_batch.assigned_user_ids IS NULL
           OR v_batch.assigned_user_ids = 'null'::jsonb THEN
            v_iterable := '[]'::jsonb;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'array' THEN
            v_iterable := v_batch.assigned_user_ids;
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'object' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(pg_catalog.to_jsonb(entry.key)
                    ORDER BY pg_catalog.octet_length(entry.key), entry.key COLLATE "C"),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.jsonb_object_keys(v_batch.assigned_user_ids)
                AS entry(key);
        ELSIF pg_catalog.jsonb_typeof(v_batch.assigned_user_ids) = 'string' THEN
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.to_jsonb(pg_catalog.substr(
                        source_text.assigned_text, positions.character_number, 1
                    )) ORDER BY positions.character_number
                ),
                '[]'::jsonb
            )
            INTO v_iterable
            FROM pg_catalog.generate_series(
                1,
                pg_catalog.char_length(v_batch.assigned_user_ids #>> '{}')
            ) AS positions(character_number)
            CROSS JOIN LATERAL (
                SELECT v_batch.assigned_user_ids #>> '{}' AS assigned_text
            ) AS source_text;
        ELSE
            RAISE EXCEPTION 'legacy assigned_user_ids is not iterable';
        END IF;

        v_updated := '[]'::jsonb;
        v_replaced := false;
        FOR v_item IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(v_iterable) AS element(value)
        LOOP
            CASE pg_catalog.jsonb_typeof(v_item)
                WHEN 'string' THEN v_string := v_item #>> '{}';
                WHEN 'null' THEN v_string := 'None';
                WHEN 'boolean' THEN
                    v_string := CASE WHEN v_item = 'true'::jsonb
                        THEN 'True' ELSE 'False' END;
                WHEN 'number' THEN v_string := aap_labeling_v1._membership_python_repr(v_item);
                WHEN 'object', 'array' THEN
                    v_string := aap_labeling_v1._membership_python_repr(v_item);
                ELSE
                    RAISE EXCEPTION 'unsupported assigned_user_ids value';
            END CASE;

            IF v_string = p_from_user_id::text THEN
                v_string := p_to_user_id::text;
                v_replaced := true;
            END IF;
            v_updated := v_updated || pg_catalog.jsonb_build_array(v_string);
        END LOOP;

        IF NOT v_replaced THEN
            v_updated := v_updated || pg_catalog.jsonb_build_array(p_to_user_id::text);
        END IF;

        UPDATE public.task_batches AS batch
        SET reviewer_id = p_to_user_id,
            assigned_user_ids = v_updated,
            updated_at = pg_catalog.now()
        WHERE batch.id = v_batch.id
          AND batch.project_id = p_project_id;
    END LOOP;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.release_membership_task_locks(
    p_project_id uuid,
    p_user_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_project_id IS NULL OR p_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    WITH active AS MATERIALIZED (
        SELECT task_lock.id
        FROM public.task_locks AS task_lock
        JOIN public.tasks AS task ON task.id = task_lock.task_id
        WHERE task.project_id = p_project_id
          AND task_lock.user_id = p_user_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
    ), deleted AS (
        DELETE FROM public.task_locks AS task_lock
        USING active
        WHERE task_lock.id = active.id
        RETURNING task_lock.id
    )
    SELECT pg_catalog.count(*)::bigint INTO v_deleted FROM deleted;

    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.release_membership_task_locks(uuid, uuid)
    FROM PUBLIC;
