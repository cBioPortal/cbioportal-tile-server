"""Databricks metadata access for slide metadata, paths, and search."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from . import meta_store

logger = logging.getLogger(__name__)
_run_query = meta_store.run_query
_param = meta_store.param


def _infer_stain_flags(stain_group: str | None, stain_name: str | None) -> tuple[bool, bool]:
    group = (stain_group or "").lower()
    name = (stain_name or "").lower()
    normalized_name = re.sub(r"\s+", " ", name).strip()
    is_hne = group in {"h&e (initial)", "h&e (other)", "h&e"} or normalized_name in {
        "h&e",
        "he",
    }
    is_ihc = group == "ihc"
    return is_hne, is_ihc


def _coerce(v: Any) -> Any:
    """Convert non-JSON-safe types (Decimal, etc.) to native Python."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _optional_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _format_patient_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "patient",
            "id": row["patient_id"],
            "label": row["patient_id"],
            "sublabel": f"{row.get('cancer_type') or ''} · {row.get('slide_count', '')} slides".strip(" ·"),
        }
        for row in rows
    ]


def _format_sample_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "sample",
            "id": row["sample_id"],
            "label": row["sample_id"],
            "sublabel": row.get("cancer_type") or "",
        }
        for row in rows
    ]


def _format_slide_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "slide",
            "id": str(row["image_id"]),
            "label": str(row["image_id"]),
            "sublabel": f"{row.get('patient_id') or ''} · {row.get('stain_name') or ''}".strip(" ·"),
        }
        for row in rows
    ]


def get_slide_dbmeta(
    image_id: str,
    warehouse_id: str,
    patient_id: str | None = None,
) -> dict | None:
    """Return flat Databricks metadata row for one slide."""
    sql = meta_store.SLIDE_SCOPED_SQL if patient_id is not None else meta_store.SLIDE_SQL
    params = [_param("image_id", str(image_id))]
    if patient_id is not None:
        params.append(_param("patient_id", str(patient_id)))
    rows = _run_query(sql, warehouse_id, params)
    if not rows:
        return None
    row = rows[0]
    image_value = row.get("image_id")
    return {
        "image_id": "" if image_value is None else str(image_value),
        "stain_name": None if row.get("stain_name") is None else str(row["stain_name"]),
        "stain_group": None if row.get("stain_group") is None else str(row["stain_group"]),
        "magnification": (
            None if row.get("magnification") is None else str(row["magnification"])
        ),
        "file_size_bytes": _optional_int(row.get("file_size_bytes")),
    }


def get_slide_path(image_id: str, warehouse_id: str) -> str | None:
    """Return the S3 URI for a slide given its image_id, or None if not found."""
    rows = _run_query(meta_store.SLIDE_PATH_SQL, warehouse_id, [_param("image_id", str(image_id))])
    if not rows:
        return None
    return rows[0].get("path") or None


def search_suggestions(query: str, warehouse_id: str) -> list[dict]:
    """
    Return up to 8 autocomplete suggestions for the given query string.

    Detects query type by pattern:
      P-<digits>            → patient suggestions
      P-<digits>-T<digits>  → sample suggestions
      <digits>              → slide image_id suggestions

    Each result has: { type, id, label, sublabel }
    """
    import re  # noqa: PLC0415

    q = query.strip()
    if not q:
        return []

    prefix = q.replace("%", r"\%").replace("_", r"\_") + "%"

    # Sample ID pattern: P-digits-T
    if re.match(r"^P-\d.*-T", q, re.IGNORECASE):
        rows = _run_query(meta_store.SEARCH_SAMPLE_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_sample_suggestions(rows)

    # Patient ID pattern: starts with P-
    if re.match(r"^P-", q, re.IGNORECASE):
        rows = _run_query(meta_store.SEARCH_PATIENT_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_patient_suggestions(rows)

    # Numeric → slide image_id
    if re.match(r"^\d", q):
        rows = _run_query(meta_store.SEARCH_SLIDE_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_slide_suggestions(rows)

    return []


# ---------------------------------------------------------------------------
# Slide summary (Phase 7)
# ---------------------------------------------------------------------------

def _sample_id_filter(sample_ids: list[str]) -> tuple[str, list]:
    placeholders = ", ".join(f":sample_id_{index}" for index in range(len(sample_ids)))
    params = [
        _param(f"sample_id_{index}", sample_id)
        for index, sample_id in enumerate(sample_ids)
    ]
    return placeholders, params

def get_sample_slide_summary(
    sample_ids: list[str],
    warehouse_id: str,
) -> list[dict]:
    """
    Return pre-computed slide availability stats for the given sample IDs.

    Reads from the ``sample_wsi_summary`` Delta table, which is populated
    nightly by the Databricks Asset Bundle job (``wsi-summary-pipeline``).

    Each result dict has:
      sample_id, patient_id, servable_slide_count,
      non_servable_hne_slide_count, non_servable_ihc_slide_count,
      has_hne, has_ihc, stain_types

    Samples not present in the summary table are silently omitted — the caller
    (generate_wsi_clinical_attrs.py) fills in zero-count rows for them.
    """
    if not sample_ids:
        return []
    placeholders, params = _sample_id_filter(sample_ids)
    rows = _run_query(
        f"""
SELECT
    sample_id,
    patient_id,
    servable_slide_count,
    non_servable_hne_slide_count,
    non_servable_ihc_slide_count,
    has_hne,
    has_ihc,
    stain_types
FROM {meta_store._SUMMARY}
WHERE sample_id IN ({placeholders})
ORDER BY sample_id
""",
        warehouse_id,
        params,
    )
    return [
        {
            "sample_id":            r.get("sample_id"),
            "patient_id":           r.get("patient_id"),
            "servable_slide_count": int(r.get("servable_slide_count") or 0),
            "non_servable_hne_slide_count": int(
                r.get("non_servable_hne_slide_count") or 0
            ),
            "non_servable_ihc_slide_count": int(
                r.get("non_servable_ihc_slide_count") or 0
            ),
            "has_hne":              int(r.get("has_hne") or 0),
            "has_ihc":              int(r.get("has_ihc") or 0),
            "stain_types":          r.get("stain_types") or "",
        }
        for r in rows
    ]


def get_live_sample_slide_summary(
    sample_ids: list[str],
    warehouse_id: str,
) -> list[dict]:
    """
    Return current patient-wide slide availability stats for the given sample IDs.

    Unlike ``get_sample_slide_summary()``, this computes counts directly from
    the cleaned diagnostic slide universe and the slide_inventory servability
    source. The matched relation is used only to map cBioPortal sample IDs to
    patients; every diagnostic slide for those patients contributes to the
    totals, including slides not matched to an IMPACT sample.
    """
    if not sample_ids:
        return []
    placeholders, params = _sample_id_filter(sample_ids)
    rows = _run_query(
        f"""
WITH selected_samples AS (
    SELECT DISTINCT
        d.sample_id AS sample_id,
        d.PATIENT_ID AS patient_id
    FROM {meta_store._TABLE} d
    WHERE d.sample_id IN ({placeholders})
      AND d.sample_id IS NOT NULL
      AND d.PATIENT_ID IS NOT NULL
),
patient_map AS (
    SELECT DISTINCT
        d.mrn AS mrn,
        d.PATIENT_ID AS patient_id
    FROM {meta_store._TABLE} d
    INNER JOIN (
        SELECT DISTINCT patient_id
        FROM selected_samples
    ) selected_patients ON d.PATIENT_ID = selected_patients.patient_id
    WHERE d.mrn IS NOT NULL
),
diagnostic_slide_universe AS (
    SELECT DISTINCT
        p.patient_id AS patient_id,
        c.image_id AS image_id,
        c.stain_name AS stain_name,
        CASE
            WHEN c.stain_group IN ('H&E (Initial)', 'H&E (Other)') THEN 'H&E'
            WHEN c.stain_group = 'IHC' THEN 'IHC'
            ELSE NULL
        END AS stain_bucket
    FROM {meta_store._CLEANED_TABLE} c
    INNER JOIN patient_map p ON c.mrn = p.mrn
    WHERE c.image_id IS NOT NULL
      AND (
        c.stain_group IN ('H&E (Initial)', 'H&E (Other)')
        OR (
            c.stain_group = 'IHC'
            AND LOWER(TRIM(COALESCE(c.stain_name, ''))) NOT LIKE 'immuno recut%'
            AND LOWER(COALESCE(c.stain_name, '')) NOT LIKE '%unstained%'
        )
      )
),
servable_inventory AS (
    SELECT DISTINCT image_id
    FROM {meta_store._INVENTORY}
    WHERE path LIKE 's3://%'
),
viewable_patient_summary AS (
SELECT
    d.PATIENT_ID AS patient_id,
    COUNT(DISTINCT d.image_id) AS servable_slide_count,
    MAX(CASE
        WHEN d.stain_group IN ('H&E (Initial)', 'H&E (Other)')
        THEN 1 ELSE 0
    END) AS has_hne,
    MAX(CASE
        WHEN d.stain_group = 'IHC'
         AND LOWER(TRIM(COALESCE(d.stain_name, ''))) NOT LIKE 'immuno recut%'
         AND LOWER(COALESCE(d.stain_name, '')) NOT LIKE '%unstained%'
        THEN 1 ELSE 0
    END) AS has_ihc,
    ARRAY_JOIN(
        ARRAY_SORT(COLLECT_SET(d.stain_name)),
        ';'
    ) AS stain_types
FROM {meta_store._TABLE} d
INNER JOIN servable_inventory s ON d.image_id = s.image_id
INNER JOIN (
    SELECT DISTINCT patient_id
    FROM selected_samples
) selected_patients ON d.PATIENT_ID = selected_patients.patient_id
WHERE d.image_id IS NOT NULL
GROUP BY d.PATIENT_ID
),
non_viewable_patient_summary AS (
SELECT
    d.patient_id AS patient_id,
    COUNT(DISTINCT CASE
        WHEN d.stain_bucket = 'H&E' AND s.image_id IS NULL THEN d.image_id
        ELSE NULL
    END) AS non_servable_hne_slide_count,
    COUNT(DISTINCT CASE
        WHEN d.stain_bucket = 'IHC' AND s.image_id IS NULL THEN d.image_id
        ELSE NULL
    END) AS non_servable_ihc_slide_count
FROM diagnostic_slide_universe d
LEFT JOIN servable_inventory s ON d.image_id = s.image_id
GROUP BY d.patient_id
)
SELECT
    selected_samples.sample_id AS sample_id,
    selected_samples.patient_id AS patient_id,
    COALESCE(viewable.servable_slide_count, 0) AS servable_slide_count,
    COALESCE(non_viewable.non_servable_hne_slide_count, 0) AS non_servable_hne_slide_count,
    COALESCE(non_viewable.non_servable_ihc_slide_count, 0) AS non_servable_ihc_slide_count,
    COALESCE(viewable.has_hne, 0) AS has_hne,
    COALESCE(viewable.has_ihc, 0) AS has_ihc,
    COALESCE(viewable.stain_types, '') AS stain_types
FROM selected_samples
LEFT JOIN viewable_patient_summary viewable
    ON selected_samples.patient_id = viewable.patient_id
LEFT JOIN non_viewable_patient_summary non_viewable
    ON selected_samples.patient_id = non_viewable.patient_id
ORDER BY selected_samples.sample_id
""",
        warehouse_id,
        params,
    )
    return [
        {
            "sample_id":            r.get("sample_id"),
            "patient_id":           r.get("patient_id"),
            "servable_slide_count": int(r.get("servable_slide_count") or 0),
            "non_servable_hne_slide_count": int(
                r.get("non_servable_hne_slide_count") or 0
            ),
            "non_servable_ihc_slide_count": int(
                r.get("non_servable_ihc_slide_count") or 0
            ),
            "has_hne":              int(r.get("has_hne") or 0),
            "has_ihc":              int(r.get("has_ihc") or 0),
            "stain_types":          r.get("stain_types") or "",
        }
        for r in rows
    ]
