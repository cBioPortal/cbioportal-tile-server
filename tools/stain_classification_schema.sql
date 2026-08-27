-- Stable input contract for the offline stain-classifier publisher.
-- The table is an approved release snapshot; missing or unapproved rows are
-- intentionally ignored by the canonical association pipeline.
CREATE TABLE IF NOT EXISTS cdsi_prod.pathology_data_mining.slide_stain_classification (
    slide_id STRING NOT NULL,
    model_name STRING NOT NULL,
    model_version STRING NOT NULL,
    scored_at TIMESTAMP NOT NULL,
    image_probability DOUBLE NOT NULL,
    image_predicted_class STRING NOT NULL,
    manual_label STRING,
    model_approved BOOLEAN NOT NULL,
    image_ihc_threshold DOUBLE NOT NULL
) USING DELTA;
