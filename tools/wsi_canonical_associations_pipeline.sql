-- WSI Canonical Association Pipeline
--
-- The output of this job is the normalized input contract consumed by the
-- cBioPortal core WSI importer. It contains pathology-only structure,
-- slide facts, and placement facts; portal clinical data stays in cBioPortal.

CREATE OR REPLACE TABLE cdsi_prod.pathology_data_mining.canonical_slide_associations AS
WITH sample_sequencing AS (
    SELECT sample_id, MAX(sequencing_date) AS sequencing_date
    FROM (
        SELECT
            SAMPLE_ID AS sample_id,
            TRY_CAST(DATE_SEQUENCING_REPORT AS DATE) AS sequencing_date
        FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.table_pathology_impact_sample_summary_dop_anno_epic_idb_combined
        WHERE SAMPLE_ID IS NOT NULL
          AND DATE_SEQUENCING_REPORT IS NOT NULL

        UNION ALL

        SELECT
            SAMPLE_ID AS sample_id,
            CAST(
                SUBSTR(DTE_TUMOR_SEQUENCING, 13, 4) || '-' ||
                CASE SUBSTR(DTE_TUMOR_SEQUENCING, 9, 3)
                    WHEN 'Jan' THEN '01' WHEN 'Feb' THEN '02'
                    WHEN 'Mar' THEN '03' WHEN 'Apr' THEN '04'
                    WHEN 'May' THEN '05' WHEN 'Jun' THEN '06'
                    WHEN 'Jul' THEN '07' WHEN 'Aug' THEN '08'
                    WHEN 'Sep' THEN '09' WHEN 'Oct' THEN '10'
                    WHEN 'Nov' THEN '11' WHEN 'Dec' THEN '12'
                END || '-' || SUBSTR(DTE_TUMOR_SEQUENCING, 6, 2) AS DATE
            ) AS sequencing_date
        FROM cdsi_prod.cdm_idbw_impact_pipeline_prod.ddp_pathology_reports
        WHERE SAMPLE_ID IS NOT NULL
          AND DTE_TUMOR_SEQUENCING IS NOT NULL
    ) source
    WHERE sequencing_date IS NOT NULL
    GROUP BY sample_id
),
inventory_paths AS (
    SELECT image_id, path
    FROM (
        SELECT
            CAST(image_id AS STRING) AS image_id,
            path,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(image_id AS STRING)
                ORDER BY
                    CASE
                        WHEN path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                        WHEN path LIKE 's3://%' THEN 1
                        ELSE 2
                    END,
                    path
            ) AS row_num
        FROM cdsi_eng_phi.pdm_base_tables.slide_inventory
        WHERE image_id IS NOT NULL
          AND path IS NOT NULL
    ) ranked
    WHERE row_num = 1
),
thumbnail_registry AS (
    SELECT image_id, artifact_uri, width, height, content_type, tile_metadata_json
    FROM (
        SELECT
            CAST(image_id AS STRING) AS image_id,
            artifact_uri,
            width,
            height,
            content_type,
            tile_metadata_json,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(image_id AS STRING)
                ORDER BY rendered_at DESC, manifest_version DESC
            ) AS row_num
        FROM cdsi_prod.pathology_data_mining.slide_thumbnail_registry
        WHERE status = 'success'
    ) ranked_thumbnails
    WHERE row_num = 1
),
sample_patient_pairs AS (
    SELECT DISTINCT PATIENT_ID AS patient_id, sample_id, mrn
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1
    WHERE PATIENT_ID IS NOT NULL AND sample_id IS NOT NULL

    UNION

    SELECT DISTINCT PATIENT_ID_IMPACT AS patient_id, SAMPLE_ID_IMPACT AS sample_id, mrn
    FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides
    WHERE PATIENT_ID_IMPACT IS NOT NULL AND SAMPLE_ID_IMPACT IS NOT NULL
),
patient_reference AS (
    SELECT patient_id, sample_id AS reference_sample_id, sequencing_date
    FROM (
        SELECT
            pairs.patient_id,
            pairs.sample_id,
            sequencing.sequencing_date,
            ROW_NUMBER() OVER (
                PARTITION BY pairs.patient_id
                ORDER BY sequencing.sequencing_date ASC, pairs.sample_id ASC
            ) AS row_num
        FROM sample_patient_pairs pairs
        INNER JOIN sample_sequencing sequencing ON sequencing.sample_id = pairs.sample_id
    ) ranked
    WHERE row_num = 1
),
patient_map AS (
    SELECT DISTINCT patient_id, mrn
    FROM sample_patient_pairs
    WHERE mrn IS NOT NULL
),
procedure_dates AS (
    SELECT surgical.ACCESSION_NUMBER AS accession_number, MAX(surgical.PROCEDURE_DATE) AS procedure_date
    FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.surgical_specimen_diagnoses_combined surgical
    INNER JOIN (
        SELECT DISTINCT cleaned.accession_number
        FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 cleaned
        INNER JOIN patient_map mapping ON mapping.mrn = cleaned.mrn
        WHERE cleaned.accession_number IS NOT NULL
    ) patient_accessions ON patient_accessions.accession_number = surgical.ACCESSION_NUMBER
    WHERE surgical.PROCEDURE_DATE IS NOT NULL
    GROUP BY surgical.ACCESSION_NUMBER
),
slide_procedure_dates AS (
    SELECT DISTINCT
        mapping.patient_id,
        CAST(cleaned.image_id AS STRING) AS image_id,
        TRY_CAST(procedure_dates.procedure_date AS DATE) AS procedure_date
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 cleaned
    INNER JOIN patient_map mapping ON mapping.mrn = cleaned.mrn
    LEFT JOIN procedure_dates ON procedure_dates.accession_number = cleaned.accession_number
    WHERE cleaned.image_id IS NOT NULL
),
block_matches AS (
    SELECT DISTINCT
        'BLOCK' AS match_level,
        d.PATIENT_ID AS patient_id,
        d.sample_id,
        CAST(d.image_id AS STRING) AS image_id,
        CASE
            WHEN CAST(d.block_id AS STRING) RLIKE '/[0-9]+-'
                THEN CONCAT('part:', REGEXP_EXTRACT(CAST(d.block_id AS STRING), '/([0-9]+)-', 1))
            ELSE CONCAT('part:image:', CAST(d.image_id AS STRING))
        END AS part_key,
        NULLIF(REGEXP_EXTRACT(CAST(d.block_id AS STRING), '/([0-9]+)-', 1), '') AS part_number,
        CAST(NULL AS STRING) AS part_designator,
        d.part_type,
        d.part_description,
        CAST(NULL AS STRING) AS subspecialty,
        d.part_description AS path_dx_title,
        CONCAT('block:', COALESCE(CAST(d.block_id AS STRING), CONCAT('image:', CAST(d.image_id AS STRING)))) AS block_key,
        CAST(d.block_id AS STRING) AS block_number,
        d.block_label,
        d.stain_name,
        d.stain_group,
        d.magnification,
        d.file_size_bytes,
        CAST(NULL AS STRING) AS barcode,
        CAST(NULL AS STRING) AS slide_type,
        COALESCE(TRY_CAST(d.dop AS DATE), fallback.procedure_date) AS procedure_date,
        inventory.path AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1 d
    LEFT JOIN inventory_paths inventory ON inventory.image_id = CAST(d.image_id AS STRING)
    LEFT JOIN slide_procedure_dates fallback
        ON fallback.patient_id = d.PATIENT_ID
       AND fallback.image_id = CAST(d.image_id AS STRING)
    WHERE d.PATIENT_ID IS NOT NULL AND d.image_id IS NOT NULL
),
part_matches AS (
    SELECT DISTINCT
        'PART' AS match_level,
        d.PATIENT_ID_IMPACT AS patient_id,
        d.SAMPLE_ID_IMPACT AS sample_id,
        CAST(d.image_id AS STRING) AS image_id,
        CONCAT('part:', COALESCE(CAST(d.PART_NUMBER AS STRING), CONCAT('image:', CAST(d.image_id AS STRING)))) AS part_key,
        CAST(d.PART_NUMBER AS STRING) AS part_number,
        CAST(NULL AS STRING) AS part_designator,
        d.part_type,
        d.part_description,
        CAST(NULL AS STRING) AS subspecialty,
        d.PATH_DX_SPEC_TITLE AS path_dx_title,
        CONCAT('block:', COALESCE(CAST(d.BLOCK_NUMBER AS STRING), d.BLOCK_LABEL, CONCAT('image:', CAST(d.image_id AS STRING)))) AS block_key,
        CAST(d.BLOCK_NUMBER AS STRING) AS block_number,
        d.block_label,
        d.stain_name,
        d.stain_group,
        d.magnification,
        d.file_size_bytes,
        CAST(NULL AS STRING) AS barcode,
        CAST(NULL AS STRING) AS slide_type,
        COALESCE(TRY_CAST(d.DATE_OF_PROCEDURE_SURGICAL AS DATE), fallback.procedure_date) AS procedure_date,
        COALESCE(inventory.path, d.SLIDE_URL) AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides d
    LEFT JOIN inventory_paths inventory ON inventory.image_id = CAST(d.image_id AS STRING)
    LEFT JOIN slide_procedure_dates fallback
        ON fallback.patient_id = d.PATIENT_ID_IMPACT
       AND fallback.image_id = CAST(d.image_id AS STRING)
    WHERE d.PATIENT_ID_IMPACT IS NOT NULL
      AND d.image_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM block_matches block_match
          WHERE block_match.patient_id = d.PATIENT_ID_IMPACT
            AND block_match.sample_id = d.SAMPLE_ID_IMPACT
            AND block_match.image_id = CAST(d.image_id AS STRING)
      )
),
matched_associations AS (
    SELECT * FROM block_matches
    UNION ALL
    SELECT * FROM part_matches
),
slide_universe AS (
    SELECT DISTINCT
        mapping.patient_id,
        CAST(cleaned.image_id AS STRING) AS image_id,
        CASE
            WHEN CAST(cleaned.block_id AS STRING) RLIKE '^.+/[0-9]+-[^/]+$'
                THEN CONCAT(
                    'part:unmatched:',
                    REGEXP_EXTRACT(CAST(cleaned.block_id AS STRING), '^(.+/[0-9]+)-[^/]+$', 1)
                )
            WHEN cleaned.block_id IS NOT NULL
                THEN CONCAT('part:unmatched:block:', CAST(cleaned.block_id AS STRING))
            ELSE CONCAT('part:unmatched:image:', CAST(cleaned.image_id AS STRING))
        END AS part_key,
        NULLIF(REGEXP_EXTRACT(CAST(cleaned.block_id AS STRING), '/([0-9]+)-', 1), '') AS part_number,
        CAST(NULL AS STRING) AS part_designator,
        cleaned.part_type,
        cleaned.part_description,
        CAST(NULL AS STRING) AS subspecialty,
        cleaned.part_description AS path_dx_title,
        CONCAT('block:', COALESCE(CAST(cleaned.block_id AS STRING), CONCAT('image:', CAST(cleaned.image_id AS STRING)))) AS block_key,
        CAST(cleaned.block_id AS STRING) AS block_number,
        cleaned.block_label,
        cleaned.stain_name,
        cleaned.stain_group,
        cleaned.magnification,
        cleaned.file_size_bytes,
        CAST(NULL AS STRING) AS barcode,
        CAST(NULL AS STRING) AS slide_type,
        procedure.procedure_date,
        inventory.path AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 cleaned
    INNER JOIN patient_map mapping ON mapping.mrn = cleaned.mrn
    LEFT JOIN inventory_paths inventory ON inventory.image_id = CAST(cleaned.image_id AS STRING)
    LEFT JOIN slide_procedure_dates procedure
        ON procedure.patient_id = mapping.patient_id
       AND procedure.image_id = CAST(cleaned.image_id AS STRING)
    WHERE cleaned.image_id IS NOT NULL
),
unmatched_associations AS (
    SELECT
        'UNMATCHED' AS match_level,
        universe.patient_id,
        CAST(NULL AS STRING) AS sample_id,
        universe.image_id,
        universe.part_key,
        universe.part_number,
        universe.part_designator,
        universe.part_type,
        universe.part_description,
        universe.subspecialty,
        universe.path_dx_title,
        universe.block_key,
        universe.block_number,
        universe.block_label,
        universe.stain_name,
        universe.stain_group,
        universe.magnification,
        universe.file_size_bytes,
        universe.barcode,
        universe.slide_type,
        universe.procedure_date,
        universe.slide_path
    FROM slide_universe universe
    WHERE NOT EXISTS (
        SELECT 1 FROM matched_associations matched
        WHERE matched.patient_id = universe.patient_id
          AND matched.image_id = universe.image_id
    )
),
canonical_associations AS (
    SELECT *
    FROM (
        SELECT
            association.*,
            ROW_NUMBER() OVER (
                PARTITION BY association.patient_id, association.image_id
                ORDER BY
                    CASE WHEN association.slide_path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                         WHEN association.slide_path LIKE 's3://%' THEN 1 ELSE 2 END,
                    CASE association.match_level WHEN 'BLOCK' THEN 0 WHEN 'PART' THEN 1 WHEN 'UNMATCHED' THEN 2 ELSE 3 END,
                    CASE WHEN association.sample_id IS NOT NULL THEN 0 ELSE 1 END,
                    COALESCE(association.part_key, '~~~~~~~~'),
                    COALESCE(association.block_key, '~~~~~~~~')
            ) AS association_row_num
        FROM (
            SELECT * FROM matched_associations
            UNION ALL
            SELECT * FROM unmatched_associations
        ) association
    ) ranked
    WHERE association_row_num = 1
),
approved_stain_predictions AS (
    SELECT slide_id, model_version, scored_at, image_probability, manual_label,
           image_ihc_threshold
    FROM (
        SELECT
            CAST(slide_id AS STRING) AS slide_id,
            model_version,
            scored_at,
            image_probability,
            manual_label,
            image_ihc_threshold,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(slide_id AS STRING)
                ORDER BY scored_at DESC, model_version DESC
            ) AS row_num
        FROM cdsi_prod.pathology_data_mining.slide_stain_classification
        WHERE model_approved = TRUE
    ) ranked_predictions
    WHERE row_num = 1
),
resolved_associations AS (
    SELECT
        association.*,
        prediction.model_version AS stain_model_version,
        prediction.scored_at AS stain_scored_at,
        prediction.image_probability AS stain_image_probability,
        CASE
            WHEN LOWER(COALESCE(prediction.manual_label, '')) IN ('he', 'ihc') THEN prediction.manual_label
            WHEN metadata_is_ihc THEN 'metadata'
            WHEN metadata_is_hne
                 AND prediction.image_probability IS NOT NULL
                 AND prediction.image_probability >= COALESCE(prediction.image_ihc_threshold, 0.90)
                THEN 'image_model'
            WHEN metadata_is_hne THEN 'metadata'
            ELSE 'metadata_fallback'
        END AS stain_classification_source,
        CASE
            WHEN LOWER(COALESCE(prediction.manual_label, '')) = 'he' THEN TRUE
            WHEN LOWER(COALESCE(prediction.manual_label, '')) = 'ihc' THEN FALSE
            ELSE metadata_is_hne
                 AND NOT metadata_is_ihc
                 AND NOT (
                     prediction.image_probability IS NOT NULL
                     AND prediction.image_probability >= COALESCE(prediction.image_ihc_threshold, 0.90)
                 )
        END AS resolved_is_hne,
        CASE
            WHEN LOWER(COALESCE(prediction.manual_label, '')) = 'ihc' THEN TRUE
            WHEN LOWER(COALESCE(prediction.manual_label, '')) = 'he' THEN FALSE
            WHEN metadata_is_ihc THEN TRUE
            WHEN metadata_is_hne
                 AND prediction.image_probability IS NOT NULL
                 AND prediction.image_probability >= COALESCE(prediction.image_ihc_threshold, 0.90)
                THEN TRUE
            ELSE FALSE
        END AS resolved_is_ihc
    FROM (
        SELECT
            association.*,
            (
                LOWER(COALESCE(association.stain_group, '')) IN ('h&e', 'h&e (initial)', 'h&e (other)')
                OR LOWER(TRIM(COALESCE(association.stain_name, ''))) IN ('h&e', 'he')
            ) AS metadata_is_hne,
            LOWER(COALESCE(association.stain_group, '')) = 'ihc' AS metadata_is_ihc
        FROM canonical_associations association
    ) association
    LEFT JOIN approved_stain_predictions prediction
        ON prediction.slide_id = CAST(association.image_id AS STRING)
)
SELECT
    'canonical_slide_associations_v3' AS association_version,
    CURRENT_TIMESTAMP() AS updated_at,
    association.match_level,
    association.patient_id,
    association.sample_id,
    reference.reference_sample_id,
    association.part_key,
    association.part_number,
    association.part_designator,
    association.part_type,
    association.part_description,
    association.subspecialty,
    association.path_dx_title,
    association.block_key,
    association.block_number,
    association.block_label,
    association.image_id,
    association.stain_name,
    association.stain_group,
    association.metadata_is_hne,
    association.metadata_is_ihc,
    association.resolved_is_hne AS is_hne,
    association.resolved_is_ihc AS is_ihc,
    association.stain_classification_source,
    association.stain_model_version,
    association.stain_scored_at,
    association.stain_image_probability,
    association.magnification,
    association.file_size_bytes,
    CASE
        WHEN association.slide_path LIKE 's3://%'
         AND thumbnail_registry.artifact_uri IS NOT NULL
         AND thumbnail_registry.tile_metadata_json IS NOT NULL
         AND TRIM(thumbnail_registry.tile_metadata_json) <> ''
         AND thumbnail_registry.width > 0
         AND thumbnail_registry.height > 0
         AND thumbnail_registry.content_type IS NOT NULL
         AND TRIM(thumbnail_registry.content_type) <> ''
        THEN TRUE
        ELSE FALSE
    END AS can_serve_tiles,
    association.barcode,
    CASE
        WHEN association.resolved_is_hne THEN 'H&E'
        WHEN association.resolved_is_ihc THEN 'IHC'
        ELSE association.slide_type
    END AS slide_type,
    CONCAT(LOWER(association.match_level), '::', association.part_key, '::', association.block_key) AS specimen_key,
    DATEDIFF(association.procedure_date, reference.sequencing_date) AS procedure_date_days,
    CASE
        WHEN association.procedure_date IS NOT NULL AND reference.sequencing_date IS NOT NULL
            THEN 'Procedure date relative to tumor sequencing'
        ELSE NULL
    END AS timepoint_source,
    association.slide_path,
    CASE
        WHEN association.slide_path LIKE 's3://%'
         AND thumbnail_registry.artifact_uri IS NOT NULL
         AND thumbnail_registry.tile_metadata_json IS NOT NULL
         AND TRIM(thumbnail_registry.tile_metadata_json) <> ''
        THEN thumbnail_registry.tile_metadata_json
        ELSE NULL
    END AS tile_metadata_json,
    CASE
        WHEN association.slide_path LIKE 's3://%'
         AND thumbnail_registry.artifact_uri IS NOT NULL
         AND thumbnail_registry.tile_metadata_json IS NOT NULL
         AND TRIM(thumbnail_registry.tile_metadata_json) <> ''
        THEN thumbnail_registry.artifact_uri
        ELSE NULL
    END AS thumbnail_url,
    thumbnail_registry.width AS thumbnail_width,
    thumbnail_registry.height AS thumbnail_height,
    thumbnail_registry.content_type AS thumbnail_content_type
FROM resolved_associations association
LEFT JOIN patient_reference reference ON reference.patient_id = association.patient_id
LEFT JOIN thumbnail_registry
    ON thumbnail_registry.image_id = CAST(association.image_id AS STRING);
