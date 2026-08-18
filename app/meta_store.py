"""Databricks SQL transport and query definitions for slide metadata."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .constants import (
    CANONICAL_ASSOCIATION_TABLE as _CANONICAL_ASSOCIATIONS,
    CLEANED_SLIDE_TABLE as _CLEANED_TABLE,
    DEID_TABLE as _TABLE,
    INVENTORY_TABLE as _INVENTORY,
    SUMMARY_TABLE as _SUMMARY,
)
from .config import settings

EXTERNAL_LINK_TIMEOUT_SEC = 120
_EXTERNAL_LINK_SAFE_HEADERS = {"accept", "range", "content-type"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Databricks external result redirects are disabled")


_external_link_opener = build_opener(_NoRedirect)


def _validated_external_link(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {item.strip().lower() for item in settings.databricks_external_result_allowed_hosts}
    if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
        raise RuntimeError("Databricks external result must use an approved HTTPS URL")
    if host not in allowed_hosts:
        raise RuntimeError("Databricks external result host is not allowlisted")
    return url


def _safe_external_headers(headers: dict[str, str] | None) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in (headers or {}).items():
        lower_name = name.lower()
        if lower_name in _EXTERNAL_LINK_SAFE_HEADERS or lower_name.startswith(("x-amz-", "x-ms-")):
            safe[name] = value
    return safe

PATIENT_SQL = f"""
WITH sample_sequencing AS (
    SELECT
        sample_id,
        MAX(sequencing_date) AS sequencing_date
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
                    WHEN 'Jan' THEN '01'
                    WHEN 'Feb' THEN '02'
                    WHEN 'Mar' THEN '03'
                    WHEN 'Apr' THEN '04'
                    WHEN 'May' THEN '05'
                    WHEN 'Jun' THEN '06'
                    WHEN 'Jul' THEN '07'
                    WHEN 'Aug' THEN '08'
                    WHEN 'Sep' THEN '09'
                    WHEN 'Oct' THEN '10'
                    WHEN 'Nov' THEN '11'
                    WHEN 'Dec' THEN '12'
                END || '-' ||
                SUBSTR(DTE_TUMOR_SEQUENCING, 6, 2) AS DATE
            ) AS sequencing_date
        FROM cdsi_prod.cdm_idbw_impact_pipeline_prod.ddp_pathology_reports
        WHERE SAMPLE_ID IS NOT NULL
          AND DTE_TUMOR_SEQUENCING IS NOT NULL
    ) x
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
                        WHEN path LIKE 's3://mskmind-bkt/reef-slides/%%' THEN 0
                        WHEN path LIKE 's3://%%' THEN 1
                        ELSE 2
                    END,
                    path
            ) AS row_num
        FROM {_INVENTORY}
        WHERE image_id IS NOT NULL
          AND path IS NOT NULL
    ) ranked_inventory
    WHERE row_num = 1
)
SELECT
    d.image_id, d.PATIENT_ID, d.sample_id,
    d.block_id, d.block_label, d.part_type, d.part_description,
    d.part_description AS path_dx_title,
    d.stain_name, d.stain_group,
    d.CANCER_TYPE, d.CANCER_TYPE_DETAILED, d.ONCOTREE_CODE,
    d.PRIMARY_SITE, d.SAMPLE_TYPE, d.METASTATIC_SITE, d.TUMOR_PURITY,
    d.ONCOGENIC_MUTATIONS, d.`#ONCOGENIC_MUTATIONS` AS NUM_ONCOGENIC_MUTATIONS,
    d.CVR_TMB_SCORE, d.MSI_TYPE, d.magnification,
    d.file_size_bytes,
    DATEDIFF(TRY_CAST(d.dop AS DATE), ss.sequencing_date) AS slide_timepoint_days,
    CASE
        WHEN TRY_CAST(d.dop AS DATE) IS NOT NULL AND ss.sequencing_date IS NOT NULL
            THEN 'Procedure date'
        ELSE NULL
    END AS slide_timepoint_source,
    inventory_paths.path AS slide_path
FROM {_TABLE} d
LEFT JOIN inventory_paths ON CAST(d.image_id AS STRING) = inventory_paths.image_id
LEFT JOIN sample_sequencing ss ON d.sample_id = ss.sample_id
WHERE d.PATIENT_ID = :patient_id
ORDER BY d.sample_id, d.block_id, d.image_id
"""

SLIDE_SQL = f"""
SELECT image_id, stain_name, stain_group, magnification, file_size_bytes
FROM {_TABLE}
WHERE image_id = :image_id
LIMIT 1
"""

SLIDE_SCOPED_SQL = f"""
SELECT image_id, stain_name, stain_group, magnification, file_size_bytes
FROM {_TABLE}
WHERE image_id = :image_id
  AND PATIENT_ID = :patient_id
LIMIT 1
"""

SLIDE_PATH_SQL = f"""
SELECT path FROM {_INVENTORY}
WHERE image_id = :image_id
LIMIT 1
"""

SEARCH_PATIENT_SQL = f"""
SELECT DISTINCT PATIENT_ID AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type,
       COUNT(*) AS slide_count
FROM {_TABLE}
WHERE PATIENT_ID LIKE :prefix
GROUP BY patient_id
ORDER BY patient_id
LIMIT 8
"""

SEARCH_SLIDE_SQL = f"""
SELECT image_id, PATIENT_ID AS patient_id, stain_name
FROM {_TABLE}
WHERE CAST(image_id AS STRING) LIKE :prefix
ORDER BY image_id
LIMIT 8
"""

SEARCH_SAMPLE_SQL = f"""
SELECT DISTINCT sample_id, PATIENT_ID AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type
FROM {_TABLE}
WHERE sample_id LIKE :prefix
GROUP BY sample_id, patient_id
ORDER BY sample_id
LIMIT 8
"""

PATIENT_ASSOCIATION_SQL = f"""
SELECT *
FROM {_CANONICAL_ASSOCIATIONS}
WHERE patient_id = :patient_id
ORDER BY image_id
"""

def param(name: str, value: str, ptype: str = "STRING"):
    from databricks.sdk.service.sql import StatementParameterListItem  # noqa: PLC0415

    return StatementParameterListItem(name=name, value=value, type=ptype)


def client():
    from databricks.sdk import WorkspaceClient  # noqa: PLC0415

    if not hasattr(client, "_inst"):
        client._inst = WorkspaceClient()  # type: ignore[attr-defined]
    return client._inst  # type: ignore[attr-defined]


def run_query(sql: str, warehouse_id: str, params: list | None = None) -> list[dict[str, Any]]:
    import time  # noqa: PLC0415

    from databricks.sdk.service.sql import StatementState  # noqa: PLC0415

    stmt = client().statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        parameters=params or [],
        wait_timeout="50s",
    )

    poll_interval = 2
    max_poll = 120
    elapsed = 0
    while stmt.status.state in (StatementState.RUNNING, StatementState.PENDING):
        if elapsed >= max_poll:
            try:
                client().statement_execution.cancel_execution(stmt.statement_id)
            except Exception:
                pass
            raise RuntimeError("Databricks query timed out")
        time.sleep(poll_interval)
        elapsed += poll_interval
        stmt = client().statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [c.name for c in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def run_query_external(sql: str, warehouse_id: str, params: list | None = None) -> list[dict[str, Any]]:
    """Run a Databricks query using paged external result links."""
    import time

    from databricks.sdk.service.sql import Disposition, Format, StatementState

    stmt = client().statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        disposition=Disposition.EXTERNAL_LINKS,
        format=Format.JSON_ARRAY,
        parameters=params or [],
        wait_timeout="50s",
    )
    poll_interval = 2
    max_poll = 120
    elapsed = 0
    while stmt.status.state in (StatementState.RUNNING, StatementState.PENDING):
        if elapsed >= max_poll:
            try:
                client().statement_execution.cancel_execution(stmt.statement_id)
            except Exception:
                pass
            raise RuntimeError("Databricks query timed out")
        time.sleep(poll_interval)
        elapsed += poll_interval
        stmt = client().statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [column.name for column in stmt.manifest.schema.columns]
    rows: list[dict[str, Any]] = []
    total_result_bytes = 0
    for chunk_index in range(int(stmt.manifest.total_chunk_count or 0)):
        chunk = client().statement_execution.get_statement_result_chunk_n(
            stmt.statement_id,
            chunk_index,
        )
        links = chunk.external_links or []
        if not links:
            continue
        link = links[0]
        if not link.external_link:
            raise RuntimeError(f"Databricks result chunk {chunk_index} has no external link")
        external_link = _validated_external_link(link.external_link)
        request = Request(external_link, headers=_safe_external_headers(link.http_headers))
        with _external_link_opener.open(request, timeout=EXTERNAL_LINK_TIMEOUT_SEC) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Databricks external result has an invalid content length") from exc
                if declared_length < 0 or declared_length > settings.databricks_external_result_max_bytes:
                    raise RuntimeError("Databricks external result exceeds byte budget")
            raw_payload = response.read(settings.databricks_external_result_max_bytes + 1)
        if len(raw_payload) > settings.databricks_external_result_max_bytes:
            raise RuntimeError("Databricks external result exceeds byte budget")
        total_result_bytes += len(raw_payload)
        if total_result_bytes > settings.databricks_external_result_max_bytes:
            raise RuntimeError("Databricks external result exceeds total byte budget")
        payload = json.loads(raw_payload.decode("utf-8"))
        rows.extend(dict(zip(columns, row)) for row in payload)
    return rows


def run_statement(sql: str, warehouse_id: str, params: list | None = None) -> None:
    """Execute a Databricks statement and wait for completion."""
    import time

    from databricks.sdk.service.sql import StatementState

    stmt = client().statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        parameters=params or [],
        wait_timeout="50s",
    )
    poll_interval = 2
    max_poll = 120
    elapsed = 0
    while stmt.status.state in (StatementState.RUNNING, StatementState.PENDING):
        if elapsed >= max_poll:
            try:
                client().statement_execution.cancel_execution(stmt.statement_id)
            except Exception:
                pass
            raise RuntimeError("Databricks statement timed out")
        time.sleep(poll_interval)
        elapsed += poll_interval
        stmt = client().statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks statement failed: {err}")


def get_patient_rows(patient_id: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(PATIENT_SQL, warehouse_id, [param("patient_id", patient_id)])


def get_patient_association_rows(
    patient_id: str, warehouse_id: str
) -> list[dict[str, Any]]:
    return run_query_external(
        PATIENT_ASSOCIATION_SQL,
        warehouse_id,
        [param("patient_id", patient_id)],
    )


def get_slide_row(image_id: str, warehouse_id: str) -> dict[str, Any] | None:
    rows = run_query(SLIDE_SQL, warehouse_id, [param("image_id", str(image_id))])
    return rows[0] if rows else None


def get_slide_path_row(image_id: str, warehouse_id: str) -> dict[str, Any] | None:
    rows = run_query(SLIDE_PATH_SQL, warehouse_id, [param("image_id", str(image_id))])
    return rows[0] if rows else None


def search_patient_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_PATIENT_SQL, warehouse_id, [param("prefix", prefix)])


def search_sample_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_SAMPLE_SQL, warehouse_id, [param("prefix", prefix)])


def search_slide_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_SLIDE_SQL, warehouse_id, [param("prefix", prefix)])


def get_sample_summary_rows(sample_ids: list[str], warehouse_id: str) -> list[dict[str, Any]]:
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
    sql = f"""
SELECT
    sample_id,
    patient_id,
    servable_slide_count,
    non_servable_hne_slide_count,
    non_servable_ihc_slide_count,
    has_hne,
    has_ihc,
    stain_types
FROM {_SUMMARY}
WHERE sample_id IN ({placeholders})
ORDER BY sample_id
"""
    return run_query(sql, warehouse_id)
