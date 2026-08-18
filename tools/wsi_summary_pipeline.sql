-- WSI Slide Summary Pipeline
-- Nightly Databricks Job: derives the legacy summary table from the canonical
-- shared pathology association snapshot.
--
-- Output table: cdsi_prod.pathology_data_mining.sample_wsi_summary
-- Job is managed via databricks.yml (Databricks Asset Bundle).

CREATE OR REPLACE TABLE cdsi_prod.pathology_data_mining.sample_wsi_summary AS
WITH canonical_rows AS (
    SELECT DISTINCT
        association_version,
        updated_at,
        sample_id,
        patient_id,
        image_id,
        slide_path,
        can_serve_tiles,
        is_hne,
        is_ihc,
        slide_type,
        stain_group,
        stain_name
    FROM cdsi_prod.pathology_data_mining.canonical_slide_associations
    WHERE sample_id IS NOT NULL
),
sample_summary AS (
    SELECT
        sample_id,
        patient_id,
        MAX(association_version) AS association_version,
        MAX(updated_at) AS updated_at,
        COUNT(
            DISTINCT CASE
                WHEN can_serve_tiles
                 AND (is_hne OR is_ihc)
                THEN image_id
                ELSE NULL
            END
        ) AS servable_slide_count,
        COUNT(
            DISTINCT CASE
                WHEN COALESCE(slide_path, '') NOT LIKE 's3://%'
                 AND is_hne
                THEN image_id
                ELSE NULL
            END
        ) AS non_servable_hne_slide_count,
        COUNT(
            DISTINCT CASE
                WHEN COALESCE(slide_path, '') NOT LIKE 's3://%'
                 AND is_ihc
                THEN image_id
                ELSE NULL
            END
        ) AS non_servable_ihc_slide_count,
        MAX(
            CASE
                WHEN can_serve_tiles
                 AND is_hne
                THEN 1
                ELSE 0
            END
        ) AS has_hne,
        MAX(
            CASE
                WHEN can_serve_tiles
                 AND is_ihc
                THEN 1
                ELSE 0
            END
        ) AS has_ihc,
        ARRAY_JOIN(
            ARRAY_SORT(
                COLLECT_SET(
                    CASE
                        WHEN can_serve_tiles AND (is_hne OR is_ihc)
                        THEN COALESCE(slide_type, stain_name)
                        ELSE NULL
                    END
                )
            ),
            ';'
        ) AS stain_types
    FROM canonical_rows
    GROUP BY sample_id, patient_id
)
SELECT *
FROM sample_summary
