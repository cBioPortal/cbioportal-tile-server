-- Read-only audit of source stain metadata coverage.
-- Run after the source tables and approved classifier release table are
-- available. The result contains coverage totals followed by conflict and
-- non-binary queues ranked by slide count.
WITH source_rows AS (
    SELECT 'block_matched' AS source_table, CAST(image_id AS STRING) AS image_id,
           stain_group, stain_name
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1
    UNION ALL
    SELECT 'part_matched', CAST(image_id AS STRING), stain_group, stain_name
    FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides
    UNION ALL
    SELECT 'cleaned_universe', CAST(image_id AS STRING), stain_group, stain_name
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2
), normalized AS (
    SELECT
        source_table,
        image_id,
        NULLIF(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REPLACE(COALESCE(CAST(stain_group AS STRING), ''), '&amp;', '&'), '[[:cntrl:]]', ' '), '[[:space:]]+', ' ')), '') AS group_clean,
        NULLIF(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REPLACE(COALESCE(CAST(stain_name AS STRING), ''), '&amp;', '&'), '[[:cntrl:]]', ' '), '[[:space:]]+', ' ')), '') AS name_clean
    FROM source_rows
), keyed AS (
    SELECT
        normalized.*,
        REGEXP_REPLACE(LOWER(COALESCE(group_clean, '')), '[^a-z0-9]+', '') AS group_key,
        REGEXP_REPLACE(LOWER(COALESCE(name_clean, '')), '[^a-z0-9]+', '') AS name_key
    FROM normalized
), classified AS (
    SELECT
        keyed.*,
        CASE
            WHEN group_key IN ('he', 'heinitial', 'heother') THEN 'H&E'
            WHEN group_key IN ('ihc', 'immunohistochemistry') THEN 'IHC'
        END AS group_class,
        CASE
            WHEN name_key IN ('he', 'hematoxylinandeosin', 'sslhe')
              OR (name_key LIKE 'recut%he' AND name_key NOT LIKE '%fish%') THEN 'H&E'
            WHEN name_key NOT LIKE '%fish%'
             AND name_key RLIKE '^(ihc|immuno|her2|pdl1|er|pr|ki67|ck[0-9]+|cd[0-9]+|gata3|androgenreceptor|yap1|egfr|idh1|chromogranin|iga|histone|keratin|kappalightchain)' THEN 'IHC'
        END AS name_class,
        CASE
            WHEN group_clean IS NULL AND name_key = 'sslhe' THEN 'curated_ssl_he'
            WHEN name_key LIKE '%fish%' THEN 'fish_exclusion'
            WHEN group_key IN ('he', 'heinitial', 'heother', 'ihc', 'immunohistochemistry') THEN 'explicit_group'
            WHEN (group_clean IS NULL OR group_key IN ('', 'nan', 'null', 'na', 'unknown'))
             AND (name_key IN ('he', 'hematoxylinandeosin') OR name_key LIKE 'recut%he'
               OR name_key RLIKE '^(ihc|immuno|her2|pdl1|er|pr|ki67|ck[0-9]+|cd[0-9]+|gata3|androgenreceptor|yap1|egfr|idh1|chromogranin|iga|histone|keratin|kappalightchain)') THEN 'blank_group_name_inference'
            ELSE 'nonbinary_or_unclassified'
        END AS policy_bucket
    FROM keyed
), approved_manual AS (
    SELECT DISTINCT
        CAST(slide_id AS STRING) AS image_id,
        LOWER(TRIM(manual_label)) AS manual_label
    FROM cdsi_prod.pathology_data_mining.slide_stain_classification
    WHERE model_approved = TRUE
      AND LOWER(TRIM(manual_label)) IN ('he', 'ihc')
), audit_rows AS (
    SELECT
        'coverage' AS section,
        source_table,
        policy_bucket AS label,
        COUNT(*) AS slides,
        COUNT(DISTINCT CONCAT(group_key, '|', name_key)) AS distinct_pairs,
        CAST(NULL AS STRING) AS stain_group,
        CAST(NULL AS STRING) AS stain_name
    FROM classified
    GROUP BY source_table, policy_bucket

    UNION ALL

    SELECT
        'manual_override',
        classified.source_table,
        approved_manual.manual_label,
        COUNT(DISTINCT classified.image_id),
        COUNT(DISTINCT CONCAT(classified.group_key, '|', classified.name_key)),
        CAST(NULL AS STRING),
        CAST(NULL AS STRING)
    FROM classified
    INNER JOIN approved_manual ON approved_manual.image_id = classified.image_id
    GROUP BY classified.source_table, approved_manual.manual_label

    UNION ALL

    SELECT
        'conflict',
        source_table,
        CONCAT(group_class, ' vs ', name_class),
        COUNT(*),
        COUNT(DISTINCT CONCAT(group_key, '|', name_key)),
        group_clean,
        name_clean
    FROM classified
    WHERE group_class IS NOT NULL AND name_class IS NOT NULL AND group_class <> name_class
    GROUP BY source_table, group_class, name_class, group_clean, name_clean

    UNION ALL

    SELECT
        'nonbinary_queue',
        source_table,
        'review',
        COUNT(*),
        COUNT(DISTINCT CONCAT(group_key, '|', name_key)),
        group_clean,
        name_clean
    FROM classified
    WHERE policy_bucket = 'nonbinary_or_unclassified'
    GROUP BY source_table, group_clean, name_clean
)
SELECT section, source_table, label, slides, distinct_pairs, stain_group, stain_name
FROM audit_rows
ORDER BY section, slides DESC, source_table, stain_group, stain_name;
