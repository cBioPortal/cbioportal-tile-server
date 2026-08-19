#!/usr/bin/env python3
"""Materialize a WSI study snapshot in an isolated Databricks namespace.

The production canonical job reads PHI-restricted source catalogs that are not
available in the Databricks development workspace.  This command loads a
validated ``meta_wsi.txt``/``data_wsi.txt`` snapshot and the offline thumbnail
registry into a dev-only schema, then derives the same canonical and summary
contracts used by cBioPortal.

It intentionally refuses the production namespace.  Set
``DATABRICKS_CONFIG_PROFILE=dev`` and pass the dev SQL warehouse explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app import meta_store
from tools.generate_slide_thumbnails import (
    RegistryRow,
    _build_manifest_from_registry,
    _registry_by_image_id,
    _publish_manifest,
)
from tools.wsi_study_format import read_wsi_study


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SOURCE_COLUMNS = (
    "patient_id",
    "reference_sample_id",
    "sample_id",
    "image_id",
    "part_key",
    "part_number",
    "part_designator",
    "part_type",
    "part_description",
    "subspecialty",
    "path_dx_title",
    "block_key",
    "block_number",
    "block_label",
    "match_level",
    "specimen_key",
    "stain_name",
    "stain_group",
    "is_hne",
    "is_ihc",
    "magnification",
    "file_size_bytes",
    "barcode",
    "slide_type",
    "procedure_date_days",
    "timepoint_source",
    "can_serve_tiles",
    "source_url",
    "tile_metadata_json",
    "thumbnail_url",
    "thumbnail_width",
    "thumbnail_height",
    "thumbnail_content_type",
)

SOURCE_TYPES = {
    "is_hne": "bool",
    "is_ihc": "bool",
    "can_serve_tiles": "bool",
    "file_size_bytes": "int",
    "procedure_date_days": "int",
    "thumbnail_width": "int",
    "thumbnail_height": "int",
}


def _identifier(value: str, label: str) -> str:
    value = value.strip()
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a simple SQL identifier")
    return value


def _namespace(value: str) -> tuple[str, str]:
    parts = value.strip().split(".")
    if len(parts) != 2 or any(not IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("--namespace must be a catalog.schema name")
    if parts[0] != "cdsi_dev":
        raise ValueError("dev snapshot materialization requires the cdsi_dev catalog")
    if not parts[1].startswith("wsi_"):
        raise ValueError("dev snapshot materialization requires a wsi_* schema")
    return parts[0], parts[1]


def _literal(value: Any, value_type: str = "str") -> str:
    if value is None or value == "":
        return "NULL"
    if value_type == "bool":
        if isinstance(value, str):
            return "TRUE" if value.strip().upper() == "TRUE" else "FALSE"
        return "TRUE" if bool(value) else "FALSE"
    if value_type == "int":
        return str(int(value))
    return "'" + str(value).replace("'", "''") + "'"


def _source_rows(rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        yield {
            "patient_id": row.get("patient_id"),
            "reference_sample_id": row.get("reference_sample_id"),
            "sample_id": row.get("sample_id"),
            "image_id": row.get("image_id"),
            "part_key": row.get("part_key"),
            "part_number": row.get("part_number"),
            "part_designator": row.get("part_designator"),
            "part_type": row.get("part_type"),
            "part_description": row.get("part_description"),
            "subspecialty": row.get("subspecialty"),
            "path_dx_title": row.get("path_dx_title"),
            "block_key": row.get("block_key"),
            "block_number": row.get("block_number"),
            "block_label": row.get("block_label"),
            "match_level": row.get("match_level"),
            "specimen_key": row.get("specimen_key"),
            "stain_name": row.get("stain_name"),
            "stain_group": row.get("stain_group"),
            "is_hne": row.get("is_hne"),
            "is_ihc": row.get("is_ihc"),
            "magnification": row.get("magnification"),
            "file_size_bytes": row.get("file_size_bytes"),
            "barcode": row.get("barcode"),
            "slide_type": row.get("slide_type"),
            "procedure_date_days": row.get("procedure_date_days"),
            "timepoint_source": row.get("timepoint_source"),
            "can_serve_tiles": row.get("can_serve_tiles"),
            "source_url": row.get("slide_path"),
            "tile_metadata_json": row.get("tile_metadata_json"),
            "thumbnail_url": row.get("thumbnail_url"),
            "thumbnail_width": row.get("thumbnail_width"),
            "thumbnail_height": row.get("thumbnail_height"),
            "thumbnail_content_type": row.get("thumbnail_content_type"),
        }


def _values(rows: Iterable[dict[str, Any]], columns: Iterable[str]) -> list[str]:
    columns = list(columns)
    return [
        "("
        + ",".join(
            _literal(row.get(column), SOURCE_TYPES.get(column, "str"))
            for column in columns
        )
        + ")"
        for row in rows
    ]


def _read_registry_records(registry_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        registry_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"registry record on line {line_number} must be an object")
        records.append(record)
    return records


def _registry_rows(records: Iterable[dict[str, Any]]) -> list[RegistryRow]:
    return [
        RegistryRow(
            image_id=str(record["image_id"]),
            source_path=str(record.get("source_path") or ""),
            artifact_uri=str(record.get("artifact_uri") or ""),
            # Failed render records intentionally have null dimensions.  Keep
            # them in the dev registry as zero-width rows so the canonical
            # derivation can exclude them without aborting the load.
            width=int(record.get("width") or 0),
            height=int(record.get("height") or 0),
            content_type=str(record.get("content_type") or "image/jpeg"),
            status=str(record.get("status") or ""),
            rendered_at=str(record.get("rendered_at") or ""),
            error_message=str(record.get("error_message") or ""),
            manifest_version=str(record.get("manifest_version") or ""),
            tile_metadata_json=str(record.get("tile_metadata_json") or ""),
        )
        for record in records
    ]


def _artifact_uri(root_uri: str, image_id: str) -> str:
    return root_uri.rstrip("/") + f"/{image_id}.jpg"


def _expected_manifest_uri(artifact_root_uri: str) -> str:
    root_uri = artifact_root_uri.strip().rstrip("/")
    parent, separator, _ = root_uri.rpartition("/")
    if not separator or not parent.endswith(":") and "://" not in parent:
        raise ValueError("--artifact-root-uri must include an object-store prefix")
    return parent + "/manifest.json"


def _validate_registry_artifacts(
    records: Iterable[dict[str, Any]], artifact_root_uri: str
) -> None:
    root_uri = artifact_root_uri.strip().rstrip("/")
    if not root_uri or "://" not in root_uri:
        raise ValueError("--artifact-root-uri must be an absolute object-store URI")

    for line_number, record in enumerate(records, 1):
        image_id = str(record.get("image_id") or "").strip()
        if not image_id:
            raise ValueError(
                f"registry record on line {line_number} is missing image_id"
            )
        artifact_uri = str(record.get("artifact_uri") or "").strip()
        if not artifact_uri:
            if str(record.get("status") or "").lower() == "success":
                raise ValueError(
                    f"successful registry record on line {line_number} is missing artifact_uri"
                )
            continue
        expected_uri = _artifact_uri(root_uri, image_id)
        if artifact_uri != expected_uri:
            raise ValueError(
                f"registry artifact on line {line_number} is outside the dev artifact root"
            )


def _validate_manifest_uri(manifest_uri: str | None, artifact_root_uri: str) -> None:
    if manifest_uri and manifest_uri.strip() != _expected_manifest_uri(
        artifact_root_uri
    ):
        raise ValueError(
            "manifest URI must be the dev artifact root's sibling manifest.json"
        )


def _create_tables(warehouse_id: str, namespace: str) -> dict[str, str]:
    catalog, schema = _namespace(namespace)
    prefix = f"{catalog}.{schema}"
    tables = {
        "source": f"{prefix}.wsi_source_snapshot",
        "registry": f"{prefix}.slide_thumbnail_registry",
        "canonical": f"{prefix}.canonical_slide_associations",
        "summary": f"{prefix}.sample_wsi_summary",
    }
    meta_store.run_statement(f"CREATE SCHEMA IF NOT EXISTS {prefix}", warehouse_id)
    for table in tables.values():
        meta_store.run_statement(f"DROP TABLE IF EXISTS {table}", warehouse_id)
    meta_store.run_statement(
        f"""CREATE TABLE {tables['source']} (
patient_id STRING, reference_sample_id STRING, sample_id STRING, image_id STRING,
part_key STRING, part_number STRING, part_designator STRING, part_type STRING,
part_description STRING, subspecialty STRING, path_dx_title STRING, block_key STRING,
block_number STRING, block_label STRING, match_level STRING, specimen_key STRING,
stain_name STRING, stain_group STRING, is_hne BOOLEAN, is_ihc BOOLEAN, magnification STRING,
file_size_bytes BIGINT, barcode STRING, slide_type STRING, procedure_date_days INT,
timepoint_source STRING, can_serve_tiles BOOLEAN, source_url STRING, tile_metadata_json STRING,
thumbnail_url STRING, thumbnail_width INT, thumbnail_height INT, thumbnail_content_type STRING
) USING DELTA""",
        warehouse_id,
    )
    meta_store.run_statement(
        f"""CREATE TABLE {tables['registry']} (
image_id STRING, source_path STRING, artifact_uri STRING, width INT, height INT,
content_type STRING, tile_metadata_json STRING, status STRING, rendered_at TIMESTAMP,
error_message STRING, manifest_version STRING
) USING DELTA""",
        warehouse_id,
    )
    return tables


def _load_source(
    warehouse_id: str, table: str, rows: list[dict[str, Any]], batch_size: int
) -> None:
    columns = list(SOURCE_COLUMNS)
    column_sql = ",".join(columns)
    for start in range(0, len(rows), batch_size):
        batch = _values(rows[start : start + batch_size], columns)
        meta_store.run_statement(
            f"INSERT INTO {table} ({column_sql}) VALUES {','.join(batch)}",
            warehouse_id,
        )


def _load_registry(
    warehouse_id: str,
    table: str,
    registry_path: Path,
    batch_size: int,
    records: list[dict[str, Any]] | None = None,
) -> list[RegistryRow]:
    records = _read_registry_records(registry_path) if records is None else records
    columns = (
        "image_id",
        "source_path",
        "artifact_uri",
        "width",
        "height",
        "content_type",
        "tile_metadata_json",
        "status",
        "rendered_at",
        "error_message",
        "manifest_version",
    )
    for start in range(0, len(records), batch_size):
        values = []
        for record in records[start : start + batch_size]:
            fields = []
            for column in columns:
                value = record.get(column)
                if column in {"width", "height"}:
                    fields.append(_literal(value, "int"))
                elif column == "rendered_at" and value:
                    fields.append("TIMESTAMP '" + str(value).replace("'", "''") + "'")
                else:
                    fields.append(_literal(value))
            values.append("(" + ",".join(fields) + ")")
        meta_store.run_statement(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES {','.join(values)}",
            warehouse_id,
        )
    return _registry_rows(records)


def _complete_registry_rows(
    source_rows: Iterable[dict[str, Any]], registry_rows: Iterable[RegistryRow]
) -> list[RegistryRow]:
    source_paths = {
        str(row["image_id"]): str(row.get("slide_path") or "") for row in source_rows
    }
    complete: list[RegistryRow] = []
    for row in _registry_by_image_id(list(registry_rows)).values():
        if (
            row.status == "success"
            and row.source_path
            and source_paths.get(row.image_id) == row.source_path
            and row.artifact_uri
            and row.width > 0
            and row.height > 0
            and row.content_type.strip()
            and row.tile_metadata_json.strip()
        ):
            complete.append(row)
    return complete


def _derive(warehouse_id: str, tables: dict[str, str]) -> None:
    source, registry, canonical, summary = (
        tables["source"],
        tables["registry"],
        tables["canonical"],
        tables["summary"],
    )
    meta_store.run_statement(
        f"""CREATE TABLE {canonical} USING DELTA AS
WITH ranked_registry AS (
  SELECT image_id, source_path, artifact_uri, width, height, content_type, tile_metadata_json,
         ROW_NUMBER() OVER (PARTITION BY image_id ORDER BY rendered_at DESC, manifest_version DESC) AS rn
  FROM {registry} WHERE status = 'success'
), source_rows AS (
  SELECT *, NULLIF(sample_id, '') AS normalized_sample_id FROM {source}
), normalized_stains AS (
  SELECT
    source_rows.*,
    NULLIF(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REPLACE(COALESCE(stain_name, ''), '&amp;', '&'), '[[:cntrl:]]', ' '), '[[:space:]]+', ' ')), '') AS stain_name_clean,
    NULLIF(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REPLACE(COALESCE(stain_group, ''), '&amp;', '&'), '[[:cntrl:]]', ' '), '[[:space:]]+', ' ')), '') AS stain_group_clean
  FROM source_rows
), stain_keys AS (
  SELECT
    normalized_stains.*,
    REGEXP_REPLACE(LOWER(COALESCE(stain_name_clean, '')), '[^a-z0-9]+', '') AS stain_name_key,
    REGEXP_REPLACE(LOWER(COALESCE(stain_group_clean, '')), '[^a-z0-9]+', '') AS stain_group_key
  FROM normalized_stains
), canonical_stains AS (
  SELECT
    stain_keys.*,
    CASE
      WHEN stain_group_key = 'he' THEN 'H&E'
      WHEN stain_group_key = 'heinitial' THEN 'H&E (Initial)'
      WHEN stain_group_key = 'heother' THEN 'H&E (Other)'
      WHEN stain_group_key IN ('ihc', 'immunohistochemistry') THEN 'IHC'
      WHEN stain_group_key IN ('nan', 'null', 'na', 'unknown') THEN NULL
      WHEN (stain_group_clean IS NULL OR stain_group_key IN ('nan', 'null', 'na', 'unknown')) AND stain_name_key = 'sslhe' THEN 'H&E (Other)'
      WHEN (stain_group_clean IS NULL OR stain_group_key IN ('nan', 'null', 'na', 'unknown')) AND stain_name_key LIKE '%fish%' THEN 'Other'
      WHEN (stain_group_clean IS NULL OR stain_group_key IN ('nan', 'null', 'na', 'unknown')) AND stain_name_key RLIKE '^(he|hematoxylin|eosin|hematoxylinandeosin|recut.*he)$' THEN 'H&E (Other)'
      WHEN (stain_group_clean IS NULL OR stain_group_key IN ('nan', 'null', 'na', 'unknown')) AND stain_name_key RLIKE '^(ihc|immuno|her2|pdl1|er|pr|ki67|ck[0-9]+|cd[0-9]+|gata3|androgenreceptor|yap1|egfr|idh1|chromogranin|iga|histone|keratin|kappalightchain)' THEN 'IHC'
      WHEN (stain_group_clean IS NULL OR stain_group_key IN ('nan', 'null', 'na', 'unknown')) AND stain_name_key RLIKE '^(impact|molecular|rna|dna|blood|normaltissue|tumor|frozensection|slidesubmitted)' THEN 'Other'
      ELSE stain_group_clean
    END AS stain_group_canonical,
    CASE
      WHEN stain_name_key = 'impacttumor' THEN 'IMPACT - Tumor'
      WHEN stain_name_key = 'impactnormaltissue' THEN 'IMPACT - Normal Tissue'
      WHEN stain_name_key = 'dmherecut' THEN 'DM H&E RECUT'
      WHEN stain_name_key = 'recutmolecularhe' THEN 'RECUT MOLECULAR H&E'
      WHEN stain_name_key = 'recutadditionalhe' THEN 'RECUT ADDITIONAL H&E'
      WHEN stain_name_key LIKE 'immunorecut%' THEN 'IMMUNO RECUT'
      WHEN stain_name_key IN ('androgenreceptorquant', 'androgenreceptornonquant') THEN 'ANDROGEN RECEPTOR'
      WHEN stain_name_key IN ('he', 'hematoxylinandeosin', 'sslhe') THEN 'H&E'
      ELSE stain_name_clean
    END AS stain_name_canonical
  FROM stain_keys
), typed_stains AS (
  SELECT canonical_stains.*,
    (
      stain_name_key NOT LIKE '%fish%'
      AND (
        stain_group_key IN ('he', 'heinitial', 'heother')
      OR ((stain_group_clean IS NULL OR stain_group_key IN ('', 'nan', 'null', 'na', 'unknown'))
          AND stain_name_key IN ('he', 'hematoxylinandeosin', 'sslhe'))
      OR ((stain_group_clean IS NULL OR stain_group_key IN ('', 'nan', 'null', 'na', 'unknown'))
          AND stain_name_key LIKE 'recut%he' AND stain_name_key NOT LIKE '%fish%')
      )
    ) AS metadata_is_hne,
    (
      (stain_group_key IN ('ihc', 'immunohistochemistry') AND stain_name_key NOT LIKE '%fish%')
      OR ((stain_group_clean IS NULL OR stain_group_key IN ('', 'nan', 'null', 'na', 'unknown'))
          AND stain_name_key NOT LIKE '%fish%'
          AND stain_name_key RLIKE '^(ihc|immuno|her2|pdl1|er|pr|ki67|ck[0-9]+|cd[0-9]+|gata3|androgenreceptor|yap1|egfr|idh1|chromogranin|iga|histone|keratin|kappalightchain)')
    ) AS metadata_is_ihc
  FROM canonical_stains
)
SELECT 'canonical_slide_associations_v4' AS association_version, CURRENT_TIMESTAMP() AS updated_at,
  s.match_level, s.patient_id, s.normalized_sample_id AS sample_id,
  NULLIF(s.reference_sample_id, '') AS reference_sample_id,
  s.part_key, s.part_number, s.part_designator, s.part_type, s.part_description,
  s.subspecialty, s.path_dx_title, s.block_key, s.block_number, s.block_label,
  s.image_id, s.stain_name_canonical AS stain_name, s.stain_group_canonical AS stain_group,
  s.stain_name_canonical, s.stain_group_canonical,
  s.stain_name AS stain_name_raw, s.stain_group AS stain_group_raw,
  s.metadata_is_hne AS is_hne, s.metadata_is_ihc AS is_ihc, s.magnification,
  s.file_size_bytes,
  CASE WHEN s.source_url LIKE 's3://%' AND r.artifact_uri IS NOT NULL
    AND r.tile_metadata_json IS NOT NULL AND TRIM(r.tile_metadata_json) <> ''
    AND r.width > 0 AND r.height > 0 AND r.content_type IS NOT NULL
    AND TRIM(r.content_type) <> '' THEN TRUE ELSE FALSE END AS can_serve_tiles,
  s.barcode, s.slide_type, s.specimen_key, s.procedure_date_days, s.timepoint_source,
  s.source_url AS slide_path,
  CASE WHEN s.source_url LIKE 's3://%' AND r.artifact_uri IS NOT NULL
    AND r.tile_metadata_json IS NOT NULL AND TRIM(r.tile_metadata_json) <> '' THEN r.tile_metadata_json END AS tile_metadata_json,
  CASE WHEN s.source_url LIKE 's3://%' AND r.artifact_uri IS NOT NULL
    AND r.tile_metadata_json IS NOT NULL AND TRIM(r.tile_metadata_json) <> '' THEN r.artifact_uri END AS thumbnail_url,
  r.width AS thumbnail_width, r.height AS thumbnail_height, r.content_type AS thumbnail_content_type
FROM typed_stains s
LEFT JOIN ranked_registry r ON r.image_id = s.image_id AND r.rn = 1 AND r.source_path = s.source_url""",
        warehouse_id,
    )
    meta_store.run_statement(
        f"""CREATE TABLE {summary} USING DELTA AS
SELECT sample_id, patient_id, MAX(association_version) AS association_version,
 MAX(updated_at) AS updated_at,
 COUNT(DISTINCT CASE WHEN can_serve_tiles AND (is_hne OR is_ihc) THEN image_id END) AS servable_slide_count,
 COUNT(DISTINCT CASE WHEN COALESCE(slide_path, '') NOT LIKE 's3://%' AND is_hne THEN image_id END) AS non_servable_hne_slide_count,
 COUNT(DISTINCT CASE WHEN COALESCE(slide_path, '') NOT LIKE 's3://%' AND is_ihc THEN image_id END) AS non_servable_ihc_slide_count,
 MAX(CASE WHEN can_serve_tiles AND is_hne THEN 1 ELSE 0 END) AS has_hne,
 MAX(CASE WHEN can_serve_tiles AND is_ihc THEN 1 ELSE 0 END) AS has_ihc,
 ARRAY_JOIN(ARRAY_SORT(COLLECT_SET(CASE WHEN can_serve_tiles AND (is_hne OR is_ihc) THEN COALESCE(slide_type, stain_name) END)), ';') AS stain_types
FROM {canonical} WHERE sample_id IS NOT NULL GROUP BY sample_id, patient_id""",
        warehouse_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-wsi", type=Path, required=True)
    parser.add_argument("--registry-jsonl", type=Path, required=True)
    parser.add_argument("--namespace", default="cdsi_dev.wsi_test")
    parser.add_argument("--warehouse-id", required=True)
    parser.add_argument("--manifest-uri")
    parser.add_argument(
        "--artifact-root-uri",
        default=os.environ.get("THUMBNAIL_ARTIFACT_ROOT_URI", ""),
        help="Dev object-store root containing {image_id}.jpg artifacts.",
    )
    parser.add_argument("--batch-size", type=int, default=1_000)
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if os.environ.get("DATABRICKS_CONFIG_PROFILE") != "dev":
        parser.error("DATABRICKS_CONFIG_PROFILE=dev is required")
    if not args.artifact_root_uri:
        parser.error("--artifact-root-uri or THUMBNAIL_ARTIFACT_ROOT_URI is required")
    try:
        _validate_manifest_uri(args.manifest_uri, args.artifact_root_uri)
    except ValueError as error:
        parser.error(str(error))

    study_id, parsed_rows = read_wsi_study(args.meta_wsi)
    source_rows = list(_source_rows(parsed_rows))
    registry_records = _read_registry_records(args.registry_jsonl)
    _validate_registry_artifacts(registry_records, args.artifact_root_uri)
    tables = _create_tables(args.warehouse_id, args.namespace)
    _load_source(args.warehouse_id, tables["source"], source_rows, args.batch_size)
    registry_rows = _load_registry(
        args.warehouse_id,
        tables["registry"],
        args.registry_jsonl,
        args.batch_size,
        records=registry_records,
    )
    _derive(args.warehouse_id, tables)

    if args.manifest_uri:
        version = "dev-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        complete_registry_rows = _complete_registry_rows(source_rows, registry_rows)
        manifest = _build_manifest_from_registry(
            complete_registry_rows, master_size=1024, manifest_version=version
        )
        _publish_manifest(args.manifest_uri, manifest, version)
        manifest_count = len(manifest["slides"])
    else:
        manifest_count = 0

    counts = meta_store.run_query(
        f"SELECT COUNT(*) AS rows, SUM(CASE WHEN can_serve_tiles THEN 1 ELSE 0 END) AS servable "
        f"FROM {tables['canonical']}",
        args.warehouse_id,
    )[0]
    print(
        f"materialized {study_id}: rows={counts['rows']} servable={counts['servable']} "
        f"registry={len(registry_rows)} manifest={manifest_count} namespace={args.namespace}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
