"""
Databricks metadata layer — patient / slide hierarchy.

Table names are defined in app/constants.py (DEID_TABLE, INVENTORY_TABLE).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import meta_store

logger = logging.getLogger(__name__)
_run_query = meta_store.run_query
_param = meta_store.param


def _infer_stain_flags(stain_group: str | None, stain_name: str | None) -> tuple[bool, bool]:
    group = (stain_group or "").lower()
    name = (stain_name or "").lower()
    normalized_name = re.sub(r"\s+", " ", name.replace("&", "&")).strip()
    is_hne = group in {"h&e (initial)", "h&e (other)", "h&e"} or normalized_name in {
        "h&e",
        "he",
    }
    is_ihc = group == "ihc"
    return is_hne, is_ihc


def _derive_block_fields(block_id: str | None, block_label: str | None) -> tuple[int | None, str, str | None]:
    part_number: int | None = None
    block_number = ""
    part_designator: str | None = None
    source = block_id or ""
    if source:
        match = re.search(r"/(\d+)-([^/]+)$", source)
        if match:
            part_number = int(match.group(1))
            raw_block = match.group(2).strip()
            label = (block_label or raw_block).strip()
            block_number_match = re.match(r"^(\d+)", raw_block)
            block_number = block_number_match.group(1) if block_number_match else raw_block
            part_designator = str(part_number)
            return part_number, block_number, label
    label = (block_label or "").strip()
    block_number_match = re.match(r"^(\d+)", label)
    block_number = block_number_match.group(1) if block_number_match else label
    return part_number, block_number, label or None


def _coerce(v: Any) -> Any:
    """Convert non-JSON-safe types (Decimal, etc.) to native Python."""
    if v is None:
        return None
    try:
        from decimal import Decimal  # noqa: PLC0415
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    return v


def _slide_sort_key(sl: dict) -> tuple:
    stain_order = {
        "h&e (initial)": 0,
        "h&e (other)": 1,
        "ihc": 2,
    }
    group = (sl.get("stain_group") or "").lower()
    order = stain_order.get(group, 3)
    return (order, (sl.get("stain_name") or "").lower())


def _new_sample(row: dict, sample_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "cancer_type": row.get("CANCER_TYPE"),
        "cancer_type_detailed": row.get("CANCER_TYPE_DETAILED"),
        "oncotree_code": row.get("ONCOTREE_CODE"),
        "primary_site": row.get("PRIMARY_SITE"),
        "sample_type": row.get("SAMPLE_TYPE"),
        "metastatic_site": row.get("METASTATIC_SITE"),
        "tumor_purity": _coerce(row.get("TUMOR_PURITY")),
        "oncogenic_mutations": row.get("ONCOGENIC_MUTATIONS"),
        "num_oncogenic_mutations": _coerce(row.get("NUM_ONCOGENIC_MUTATIONS")),
        "tmb_score": _coerce(row.get("CVR_TMB_SCORE")),
        "msi_type": row.get("MSI_TYPE"),
        "parts": {},
    }


def _new_part(row: dict, part_number: int | None) -> dict:
    path_dx_title = row.get("path_dx_title") or row.get("PATH_DX_SPEC_TITLE")
    return {
        "part_number": part_number,
        "part_designator": str(part_number) if part_number is not None else None,
        "part_type": row.get("part_type"),
        "part_description": row.get("part_description"),
        "subspecialty": None,
        "path_dx_title": path_dx_title,
        "blocks": {},
    }


def _new_slide(row: dict, block_label: str | None, block_number: str, is_hne: bool, is_ihc: bool, can_serve: bool) -> dict:
    path_dx_title = row.get("path_dx_title") or row.get("PATH_DX_SPEC_TITLE")
    slide_timepoint_days = _coerce(row.get("slide_timepoint_days"))
    if slide_timepoint_days is not None:
        slide_timepoint_days = int(slide_timepoint_days)
    return {
        "image_id": str(row.get("image_id")),
        "stain_name": row.get("stain_name"),
        "stain_group": row.get("stain_group"),
        "is_hne": is_hne,
        "is_ihc": is_ihc,
        "magnification": row.get("magnification"),
        "file_size_bytes": _coerce(row.get("file_size_bytes")),
        "can_serve_tiles": can_serve,
        "barcode": None,
        "block_label": block_label,
        "block_number": block_number,
        "part_description": row.get("part_description"),
        "path_dx_title": path_dx_title,
        "slide_timepoint_days": slide_timepoint_days,
        "slide_timepoint_source": row.get("slide_timepoint_source"),
    }


def _build_specimen_key(
    row: dict, part_number: int | None, block_number: str
) -> str:
    match_level = (row.get("match_level") or "UNMATCHED").upper()
    part_token = str(part_number) if part_number is not None else "?"
    if match_level == "BLOCK":
        return f"block::{part_token}::{block_number or '?'}"
    if match_level == "PART":
        return f"part::{part_token}"
    return f"unmatched::{part_token}::{block_number or '?'}"


def _association_identity(
    row: dict[str, Any],
    part_number: int | None,
    block_number: str,
    can_serve: bool,
    slide_timepoint_days: int | None,
) -> tuple[Any, ...]:
    return (
        str(row.get("image_id")),
        row.get("sample_id"),
        (row.get("match_level") or "UNMATCHED").upper(),
        _build_specimen_key(row, part_number, block_number),
        can_serve,
        slide_timepoint_days,
    )


def _association_path_rank(slide_path: str | None) -> int:
    path = slide_path or ""
    if path.startswith("s3://mskmind-bkt/reef-slides/"):
        return 0
    if path.startswith("s3://"):
        return 1
    return 2


def _association_match_rank(match_level: str | None) -> int:
    normalized = (match_level or "UNMATCHED").upper()
    if normalized == "BLOCK":
        return 0
    if normalized == "PART":
        return 1
    if normalized == "UNMATCHED":
        return 2
    return 3


def _canonical_association_preference(
    row: dict[str, Any],
) -> tuple[Any, ...]:
    part_number, block_number, _ = _derive_block_fields(
        row.get("block_id"),
        row.get("block_label"),
    )
    part_token = (
        f"{int(part_number):08d}" if isinstance(part_number, int) else "~~~~~~~~"
    )
    block_token = block_number or "~~~~~~~~"
    return (
        _association_path_rank(row.get("slide_path")),
        _association_match_rank(row.get("match_level")),
        0 if row.get("sample_id") else 1,
        str(row.get("sample_id") or "~~~~~~~~"),
        part_token,
        block_token,
        str(row.get("stain_group") or "~~~~~~~~"),
        str(row.get("stain_name") or "~~~~~~~~"),
        0 if row.get("part_description") else 1,
        str(row.get("part_description") or "~~~~~~~~"),
        0 if row.get("slide_timepoint_days") is not None else 1,
        str(row.get("image_id") or ""),
    )


def _canonicalize_association_rows(
    rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    def _timepoint_sort_value(row: dict[str, Any]) -> float:
        value = row.get("slide_timepoint_days")
        if value is None:
            return float("inf")
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("inf")

    best_by_image_id: dict[str, dict[str, Any]] = {}

    for row in rows:
        image_id = str(row.get("image_id") or "").strip()
        if not image_id:
            continue

        existing = best_by_image_id.get(image_id)
        if existing is None:
            best_by_image_id[image_id] = row
            continue

        if _canonical_association_preference(row) < _canonical_association_preference(
            existing
        ):
            best_by_image_id[image_id] = row

    canonical_rows = list(best_by_image_id.values())
    canonical_rows.sort(
        key=lambda row: (
            str(row.get("sample_id") or ""),
            _association_match_rank(row.get("match_level")),
            _timepoint_sort_value(row),
            str(row.get("image_id") or ""),
        )
    )
    return canonical_rows


def _assemble_slide_associations(rows: list[dict[str, Any]]) -> tuple[list[dict], str | None, str | None]:
    associations: list[dict] = []
    reference_sample_id: str | None = None
    reference_sequencing_date: str | None = None
    seen_associations: set[tuple[Any, ...]] = set()

    for row in rows:
        part_number, derived_block_number, derived_block_label = _derive_block_fields(
            row.get("block_id"),
            row.get("block_label"),
        )
        block_number = str(derived_block_number or "")
        slide_url = row.get("slide_path") or ""
        can_serve = slide_url.startswith("s3://")
        is_hne, is_ihc = _infer_stain_flags(row.get("stain_group"), row.get("stain_name"))
        slide_timepoint_days = _coerce(row.get("slide_timepoint_days"))
        if slide_timepoint_days is not None:
            slide_timepoint_days = int(slide_timepoint_days)
        association_identity = _association_identity(
            row,
            part_number,
            block_number,
            can_serve,
            slide_timepoint_days,
        )

        if reference_sample_id is None:
            reference_sample_id = row.get("reference_sample_id")
        if reference_sequencing_date is None and row.get("reference_sequencing_date") is not None:
            reference_sequencing_date = str(row.get("reference_sequencing_date"))
        if association_identity in seen_associations:
            continue
        seen_associations.add(association_identity)

        associations.append(
            {
                "image_id": str(row.get("image_id")),
                "sample_id": row.get("sample_id"),
                "match_level": (row.get("match_level") or "UNMATCHED").upper(),
                "specimen_key": _build_specimen_key(row, part_number, block_number),
                "part_number": str(part_number) if part_number is not None else None,
                "part_description": row.get("part_description"),
                "block_number": block_number or None,
                "block_label": derived_block_label,
                "slide_type": "IHC" if is_ihc else "H&E",
                "stain_name": row.get("stain_name"),
                "procedure_date_days": slide_timepoint_days,
                "timepoint_source": row.get("slide_timepoint_source"),
                "can_serve_tiles": can_serve,
            }
        )

    return associations, reference_sample_id, reference_sequencing_date


def _merge_association_rows_into_hierarchy(
    hierarchy: dict,
    rows: list[dict[str, Any]],
) -> None:
    sample_map = {sample["sample_id"]: sample for sample in hierarchy["samples"]}
    seen_slide_keys = set()

    for sample in hierarchy["samples"]:
        for part in sample["parts"]:
            for block in part["blocks"]:
                for slide in block["slides"]:
                    seen_slide_keys.add((sample["sample_id"], slide["image_id"]))

    for row in rows:
        raw_sample_id = row.get("sample_id")
        sample_id = raw_sample_id or "UNMATCHED"
        image_id = str(row.get("image_id"))
        slide_key = (sample_id, image_id)
        if slide_key in seen_slide_keys:
            continue

        part_number, derived_block_number, derived_block_label = _derive_block_fields(
            row.get("block_id"),
            row.get("block_label"),
        )
        block_number = str(derived_block_number or "")
        slide_url = row.get("slide_path") or ""
        can_serve = slide_url.startswith("s3://")
        is_hne, is_ihc = _infer_stain_flags(
            row.get("stain_group"), row.get("stain_name")
        )

        sample = sample_map.get(sample_id)
        if sample is None:
            sample = _new_sample(row, sample_id)
            if raw_sample_id is None:
                sample["sample_type"] = "Unmatched pathology slides"
            sample["parts"] = {}
            sample_map[sample_id] = sample
            hierarchy["samples"].append(sample)

        if isinstance(sample.get("parts"), list):
            sample["parts"] = {
                str(part.get("part_number") or "?"): {
                    **{k: v for k, v in part.items() if k != "blocks"},
                    "blocks": {
                        str(block.get("block_number") or ""): block
                        for block in part.get("blocks", [])
                    },
                }
                for part in sample["parts"]
            }

        part_key = str(part_number) if part_number is not None else "?"
        part = sample["parts"].setdefault(part_key, _new_part(row, part_number))

        if isinstance(part.get("blocks"), list):
            part["blocks"] = {
                str(block.get("block_number") or ""): block
                for block in part["blocks"]
            }

        block = part["blocks"].setdefault(
            block_number,
            {
                "block_number": block_number,
                "block_label": derived_block_label,
                "slides": [],
            },
        )
        block["slides"].append(
            _new_slide(
                row, derived_block_label, block_number, is_hne, is_ihc, can_serve
            )
        )
        seen_slide_keys.add(slide_key)

    normalized_samples: list[dict] = []
    for sample in hierarchy["samples"]:
        if isinstance(sample.get("parts"), dict):
            parts_list = []
            for part in sample["parts"].values():
                if isinstance(part.get("blocks"), dict):
                    blocks_list = []
                    for block in part["blocks"].values():
                        block["slides"].sort(key=_slide_sort_key)
                        blocks_list.append(block)
                    part = {k: v for k, v in part.items() if k != "blocks"} | {
                        "blocks": blocks_list
                    }
                parts_list.append(part)
            sample = {k: v for k, v in sample.items() if k != "parts"} | {
                "parts": parts_list
            }
        normalized_samples.append(sample)

    hierarchy["samples"] = normalized_samples


def _assemble_patient_hierarchy(rows: list[dict[str, Any]], patient_id: str) -> dict:
    samples: dict[str, dict] = {}

    for row in rows:
        sample_id = row.get("sample_id") or ""
        part_number, derived_block_number, derived_block_label = _derive_block_fields(
            row.get("block_id"),
            row.get("block_label"),
        )
        block_number = str(derived_block_number or "")
        part_key = str(part_number) if part_number is not None else "?"
        slide_url = row.get("slide_path") or ""
        can_serve = slide_url.startswith("s3://")
        is_hne, is_ihc = _infer_stain_flags(row.get("stain_group"), row.get("stain_name"))

        sample = samples.setdefault(sample_id, _new_sample(row, sample_id))
        part = sample["parts"].setdefault(part_key, _new_part(row, part_number))
        block = part["blocks"].setdefault(
            block_number,
            {
                "block_number": block_number,
                "block_label": derived_block_label,
                "slides": [],
            },
        )
        block["slides"].append(
            _new_slide(row, derived_block_label, block_number, is_hne, is_ihc, can_serve)
        )

    result: list[dict] = []
    for sample in samples.values():
        parts_list = []
        for part in sample["parts"].values():
            blocks_list = []
            for block in part["blocks"].values():
                block["slides"].sort(key=_slide_sort_key)
                blocks_list.append(block)
            blocks_list.sort(key=lambda block: (0 if not (block.get("block_label") or "").strip() else 1))
            part_out = {k: v for k, v in part.items() if k != "blocks"}
            part_out["blocks"] = blocks_list
            parts_list.append(part_out)
        sample_out = {k: v for k, v in sample.items() if k != "parts"}
        sample_out["parts"] = parts_list
        result.append(sample_out)

    return {"patient_id": patient_id, "samples": result}


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


def get_patient_hierarchy(patient_id: str, warehouse_id: str) -> dict | None:
    """
    Return a nested hierarchy dict:
      { patient_id, samples: [{ sample_id, cancer_type, …, parts: [{ part_number, …,
        blocks: [{ block_number, …, slides: [{ image_id, stain_name, … }] }] }] }] }
    Returns None if patient is not found in the table.
    """
    rows = _run_query(meta_store.PATIENT_SQL, warehouse_id, [_param("patient_id", patient_id)])
    association_rows = _canonicalize_association_rows(
        meta_store.get_patient_association_rows(
        patient_id,
        warehouse_id,
        )
    )
    if not rows and not association_rows:
        return None
    hierarchy = (
        _assemble_patient_hierarchy(rows, patient_id)
        if rows
        else {"patient_id": patient_id, "samples": []}
    )
    _merge_association_rows_into_hierarchy(hierarchy, association_rows)
    (
        hierarchy["slide_associations"],
        hierarchy["reference_sample_id"],
        hierarchy["reference_sequencing_date"],
    ) = _assemble_slide_associations(association_rows)
    return hierarchy


def get_slide_dbmeta(image_id: str, warehouse_id: str) -> dict | None:
    """Return flat Databricks metadata row for one slide."""
    rows = _run_query(meta_store.SLIDE_SQL, warehouse_id, [_param("image_id", str(image_id))])
    if not rows:
        return None
    return {k: _coerce(v) for k, v in rows[0].items()}


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
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
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
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
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
