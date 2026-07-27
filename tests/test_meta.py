"""Tests for Databricks metadata layer (app/meta.py)."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from app import meta_store
from app.meta import (
    _coerce,
    _infer_stain_flags,
    get_patient_hierarchy,
    get_slide_path,
    search_suggestions,
)


# ---------------------------------------------------------------------------
# _coerce
# ---------------------------------------------------------------------------

class TestCoerce:
    def test_none_stays_none(self):
        assert _coerce(None) is None

    def test_decimal_becomes_float(self):
        assert _coerce(Decimal("3.14")) == pytest.approx(3.14)
        assert isinstance(_coerce(Decimal("3.14")), float)

    def test_int_unchanged(self):
        assert _coerce(42) == 42

    def test_float_unchanged(self):
        assert _coerce(3.14) == pytest.approx(3.14)

    def test_string_unchanged(self):
        assert _coerce("hello") == "hello"


class TestInferStainFlags:
    @pytest.mark.parametrize(
        "stain_name",
        ["IMMUNO RECUT", "Immuno Recut L1", "DM UNSTAINED RECUT"],
    )
    def test_generic_ihc_preparations_follow_the_source_ihc_group(self, stain_name):
        assert _infer_stain_flags("IHC", stain_name) == (False, True)

    @pytest.mark.parametrize(
        "stain_name",
        ["ER (Quant)", "Her2Neu (Quant)", "Ki-67 (MIB-1)", "PD-L1(SP263)"],
    )
    def test_named_ihc_markers_remain_ihc(self, stain_name):
        assert _infer_stain_flags("IHC", stain_name) == (False, True)

    @pytest.mark.parametrize(
        ("stain_group", "stain_name"),
        [
            ("Histology", "H&E"),
            ("Histology", "HE"),
            ("H&E (Initial)", "Histology"),
        ],
    )
    def test_histology_style_stains_are_treated_as_hne(
        self, stain_group, stain_name
    ):
        assert _infer_stain_flags(stain_group, stain_name) == (True, False)


# ---------------------------------------------------------------------------
# search_suggestions — query routing
# ---------------------------------------------------------------------------

class TestSearchSuggestionsRouting:
    """search_suggestions picks the correct SQL branch based on the query pattern."""

    def _call(self, q, rows=None):
        rows = rows or []
        with patch("app.meta._run_query", return_value=rows) as mock_rq:
            results = search_suggestions(q, warehouse_id="wh-test")
        return results, mock_rq

    def test_empty_query_returns_empty_without_db(self):
        results, mock_rq = self._call("")
        assert results == []
        mock_rq.assert_not_called()

    def test_whitespace_only_returns_empty(self):
        results, mock_rq = self._call("   ")
        assert results == []
        mock_rq.assert_not_called()

    def test_unrecognized_pattern_returns_empty(self):
        results, mock_rq = self._call("some-random-text")
        assert results == []
        mock_rq.assert_not_called()

    def test_patient_prefix_queries_patient_sql(self):
        rows = [{"patient_id": "P-1234567", "cancer_type": "CRC", "slide_count": 5}]
        results, mock_rq = self._call("P-12", rows)
        sql = mock_rq.call_args[0][0]
        assert "PATIENT_ID LIKE :prefix" in sql
        assert results[0]["type"] == "patient"
        assert results[0]["id"]   == "P-1234567"
        assert "sublabel" in results[0]

    def test_sample_prefix_queries_sample_sql(self):
        rows = [{"sample_id": "P-1234-T01-IM6", "patient_id": "P-1234", "cancer_type": "NSCLC"}]
        results, mock_rq = self._call("P-1234-T", rows)
        sql = mock_rq.call_args[0][0]
        assert "sample_id LIKE :prefix" in sql
        assert results[0]["type"]  == "sample"
        assert results[0]["id"]    == "P-1234-T01-IM6"

    def test_numeric_prefix_queries_slide_sql(self):
        rows = [{"image_id": 1492807, "patient_id": "P-1234", "stain_name": "H&E"}]
        results, mock_rq = self._call("149", rows)
        sql = mock_rq.call_args[0][0]
        assert "image_id" in sql.lower()
        assert results[0]["type"]  == "slide"
        assert results[0]["id"]    == "1492807"

    def test_patient_sublabel_contains_slide_count(self):
        rows = [{"patient_id": "P-0001", "cancer_type": "Lung", "slide_count": 12}]
        results, _ = self._call("P-0001", rows)
        assert "12" in results[0]["sublabel"]

    def test_empty_rows_returns_empty_list(self):
        results, _ = self._call("P-9999", rows=[])
        assert results == []

    def test_prefix_wildcard_escaping(self):
        """% and _ in queries must be escaped before passing to SQL LIKE."""
        with patch("app.meta._run_query", return_value=[]) as mock_rq:
            search_suggestions("P-10%x_y", warehouse_id="wh")
        # The SQL param value should have the % and _ escaped
        call_params = mock_rq.call_args[0][2]   # third positional arg = params list
        param_value = call_params[0].value
        assert r"\%" in param_value
        assert r"\_" in param_value


# ---------------------------------------------------------------------------
# get_patient_hierarchy — hierarchy assembly
# ---------------------------------------------------------------------------

def _base_row(**overrides):
    row = {
        "image_id":                1,
        "PATIENT_ID":              "P-0001",
        "sample_id":               "P-0001-T01-IM6",
        "block_id":                "specimen/1-A1",
        "part_type":               "Primary",
        "part_description":        "Colon",
        "block_label":             "A1",
        "stain_name":              "H&E",
        "stain_group":             "H&E (Initial)",
        "CANCER_TYPE":             "Colorectal Cancer",
        "CANCER_TYPE_DETAILED":    "Colon Adenocarcinoma",
        "ONCOTREE_CODE":           "COAD",
        "PRIMARY_SITE":            "Colon",
        "SAMPLE_TYPE":             "Primary",
        "METASTATIC_SITE":         None,
        "TUMOR_PURITY":            Decimal("0.7"),
        "ONCOGENIC_MUTATIONS":     "TP53 p.R248W",
        "NUM_ONCOGENIC_MUTATIONS": Decimal("1"),
        "CVR_TMB_SCORE":           None,
        "MSI_TYPE":                "MSS",
        "magnification":           20,
        "file_size_bytes":         1_234_567,
        "slide_timepoint_days":    -20,
        "slide_timepoint_source":  "Procedure date",
        "PATH_DX_SPEC_TITLE":      "Colon resection",
        "PATH_DX_SPEC_DESC":       None,
        "slide_path":              "s3://mskmind-bkt/reef-slides/1.svs",
    }
    row.update(overrides)
    return row


def _hierarchy(rows):
    with (
        patch("app.meta._run_query", return_value=rows),
        patch("app.meta.meta_store.get_patient_association_rows", return_value=rows),
    ):
        return get_patient_hierarchy("P-0001", "wh-test")


class TestGetPatientHierarchy:
    def test_none_for_empty_rows(self):
        assert _hierarchy([]) is None

    def test_patient_id_preserved(self):
        result = _hierarchy([_base_row()])
        assert result["patient_id"] == "P-0001"

    def test_single_slide_full_nesting(self):
        result = _hierarchy([_base_row()])
        s = result["samples"][0]
        assert s["sample_id"]    == "P-0001-T01-IM6"
        assert s["oncotree_code"] == "COAD"
        p = s["parts"][0]
        assert p["path_dx_title"] == "Colon resection"
        b = p["blocks"][0]
        sl = b["slides"][0]
        assert sl["image_id"]       == "1"
        assert sl["is_hne"]         is True
        assert sl["is_ihc"]         is False
        assert sl["can_serve_tiles"] is True
        assert sl["path_dx_title"] == "Colon resection"
        assert sl["slide_timepoint_days"] == -20
        assert sl["slide_timepoint_source"] == "Procedure date"

    def test_missing_slide_timepoint_fields_stay_null(self):
        result = _hierarchy(
            [
                _base_row(
                    slide_timepoint_days=None,
                    slide_timepoint_source=None,
                )
            ]
        )
        sl = result["samples"][0]["parts"][0]["blocks"][0]["slides"][0]
        assert sl["slide_timepoint_days"] is None
        assert sl["slide_timepoint_source"] is None

    def test_unservable_slide_has_false_can_serve(self):
        result = _hierarchy([_base_row(slide_path="")])
        sl = result["samples"][0]["parts"][0]["blocks"][0]["slides"][0]
        assert sl["can_serve_tiles"] is False

    def test_decimal_tumor_purity_coerced_to_float(self):
        result = _hierarchy([_base_row()])
        assert isinstance(result["samples"][0]["tumor_purity"], float)
        assert result["samples"][0]["tumor_purity"] == pytest.approx(0.7)

    def test_two_samples_in_separate_buckets(self):
        rows = [
            _base_row(sample_id="P-0001-T01-IM6", image_id=1),
            _base_row(sample_id="P-0001-T02-IM6", image_id=2),
        ]
        result = _hierarchy(rows)
        sample_ids = {s["sample_id"] for s in result["samples"]}
        assert sample_ids == {"P-0001-T01-IM6", "P-0001-T02-IM6"}

    def test_two_slides_same_block(self):
        rows = [_base_row(image_id=1), _base_row(image_id=2)]
        result = _hierarchy(rows)
        slides = result["samples"][0]["parts"][0]["blocks"][0]["slides"]
        assert len(slides) == 2

    def test_hne_sorts_before_ihc(self):
        rows = [
            _base_row(image_id=2, stain_name="PD-L1", stain_group="IHC"),
            _base_row(image_id=1, stain_name="H&E", stain_group="H&E (Initial)"),
        ]
        result = _hierarchy(rows)
        slides = result["samples"][0]["parts"][0]["blocks"][0]["slides"]
        assert slides[0]["stain_name"] == "H&E"
        assert slides[1]["stain_name"] == "PD-L1"

    def test_samples_is_list_not_dict(self):
        result = _hierarchy([_base_row()])
        assert isinstance(result["samples"], list)
        assert isinstance(result["samples"][0]["parts"], list)
        assert isinstance(result["samples"][0]["parts"][0]["blocks"], list)

    def test_duplicate_slide_associations_are_deduplicated(self):
        rows = [
            _base_row(
                image_id=1,
                match_level="BLOCK",
                reference_sample_id="P-0001-T01-IM6",
                reference_sequencing_date="2024-01-20",
            ),
            _base_row(
                image_id=1,
                match_level="BLOCK",
                reference_sample_id="P-0001-T01-IM6",
                reference_sequencing_date="2024-01-20",
            ),
        ]

        result = _hierarchy(rows)

        assert result["slide_associations"] == [
            {
                "image_id": "1",
                "sample_id": "P-0001-T01-IM6",
                "match_level": "BLOCK",
                "specimen_key": "block::1::A1",
                "part_number": "1",
                "part_description": "Colon",
                "block_number": "A1",
                "block_label": "A1",
                "slide_type": "H&E",
                "stain_name": "H&E",
                "procedure_date_days": -20,
                "timepoint_source": "Procedure date",
                "can_serve_tiles": True,
            }
        ]

    def test_conflicting_slide_association_buckets_prefer_block_match(self):
        association_rows = [
            _base_row(
                image_id=1,
                sample_id="P-0001-T01-IM6",
                match_level="PART",
                block_id="specimen/1-A1",
                block_label="A1",
                reference_sample_id="P-0001-T01-IM6",
                reference_sequencing_date="2024-01-20",
            ),
            _base_row(
                image_id=1,
                sample_id="P-0001-T01-IM6",
                match_level="BLOCK",
                block_id="specimen/1-A1",
                block_label="A1",
                reference_sample_id="P-0001-T01-IM6",
                reference_sequencing_date="2024-01-20",
            ),
        ]

        with patch("app.meta._run_query", return_value=[]):
            with patch(
                "app.meta.meta_store.get_patient_association_rows",
                return_value=association_rows,
            ):
                result = get_patient_hierarchy("P-0001", "wh-test")

        assert result is not None
        assert result["slide_associations"] == [
            {
                "image_id": "1",
                "sample_id": "P-0001-T01-IM6",
                "match_level": "BLOCK",
                "specimen_key": "block::1::A1",
                "part_number": "1",
                "part_description": "Colon",
                "block_number": "A1",
                "block_label": "A1",
                "slide_type": "H&E",
                "stain_name": "H&E",
                "procedure_date_days": -20,
                "timepoint_source": "Procedure date",
                "can_serve_tiles": True,
            }
        ]
        assert len(result["samples"]) == 1
        assert result["samples"][0]["sample_id"] == "P-0001-T01-IM6"
        assert len(result["samples"][0]["parts"]) == 1
        assert len(result["samples"][0]["parts"][0]["blocks"]) == 1
        assert len(result["samples"][0]["parts"][0]["blocks"][0]["slides"]) == 1


# ---------------------------------------------------------------------------
# get_slide_path
# ---------------------------------------------------------------------------

class TestGetSlidePath:
    def _call(self, rows):
        with patch("app.meta._run_query", return_value=rows):
            return get_slide_path("474017", warehouse_id="wh-test")

    def test_returns_path_when_found(self):
        result = self._call([{"path": "s3://mskmind-bkt/reef-slides/474017.svs"}])
        assert result == "s3://mskmind-bkt/reef-slides/474017.svs"

    def test_returns_none_when_no_rows(self):
        assert self._call([]) is None

    def test_returns_none_when_path_is_empty(self):
        assert self._call([{"path": ""}]) is None

    def test_returns_none_when_path_is_null(self):
        assert self._call([{"path": None}]) is None


# ---------------------------------------------------------------------------
# get_sample_slide_summary
# ---------------------------------------------------------------------------

from app.meta import get_live_sample_slide_summary, get_sample_slide_summary  # noqa: E402


class TestGetSampleSlideSummary:
    """Tests for get_sample_slide_summary — reads from sample_wsi_summary Delta table."""

    def _call(self, sample_ids, rows):
        with patch("app.meta._run_query", return_value=rows) as mock_rq:
            result = get_sample_slide_summary(sample_ids, warehouse_id="wh-test")
        return result, mock_rq

    # ------------------------------------------------------------------
    # Empty / edge-case inputs
    # ------------------------------------------------------------------

    def test_empty_sample_ids_returns_empty_without_query(self):
        result, mock_rq = self._call([], rows=[])
        assert result == []
        mock_rq.assert_not_called()

    def test_no_rows_returns_empty_list(self):
        result, _ = self._call(["P-001-T01"], rows=[])
        assert result == []

    # ------------------------------------------------------------------
    # Return-value structure
    # ------------------------------------------------------------------

    def _sample_row(
        self,
        sample_id="P-001-T01",
        patient_id="P-001",
        servable_slide_count=3,
        non_servable_hne_slide_count=0,
        non_servable_ihc_slide_count=0,
        has_hne=1,
        has_ihc=0,
        stain_types="H&E",
    ):
        return {
            "sample_id":            sample_id,
            "patient_id":           patient_id,
            "servable_slide_count": servable_slide_count,
            "non_servable_hne_slide_count": non_servable_hne_slide_count,
            "non_servable_ihc_slide_count": non_servable_ihc_slide_count,
            "has_hne":              has_hne,
            "has_ihc":              has_ihc,
            "stain_types":          stain_types,
        }

    def test_single_row_keys(self):
        result, _ = self._call(["P-001-T01"], rows=[self._sample_row()])
        assert len(result) == 1
        row = result[0]
        assert set(row.keys()) == {
            "sample_id", "patient_id", "servable_slide_count",
            "non_servable_hne_slide_count", "non_servable_ihc_slide_count",
            "has_hne", "has_ihc", "stain_types",
        }

    def test_counts_are_ints(self):
        result, _ = self._call(["P-001-T01"], rows=[self._sample_row(servable_slide_count="4")])
        assert isinstance(result[0]["servable_slide_count"], int)
        assert result[0]["servable_slide_count"] == 4

    def test_has_hne_is_int(self):
        result, _ = self._call(["P-001-T01"], rows=[self._sample_row(has_hne="1")])
        assert result[0]["has_hne"] == 1

    def test_has_ihc_is_int(self):
        result, _ = self._call(["P-001-T01"], rows=[self._sample_row(has_ihc="1")])
        assert result[0]["has_ihc"] == 1

    def test_non_servable_counts_are_ints(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[
                self._sample_row(
                    non_servable_hne_slide_count="2",
                    non_servable_ihc_slide_count="5",
                )
            ],
        )
        assert result[0]["non_servable_hne_slide_count"] == 2
        assert result[0]["non_servable_ihc_slide_count"] == 5

    def test_stain_types_none_becomes_empty_string(self):
        result, _ = self._call(["P-001-T01"], rows=[self._sample_row(stain_types=None)])
        assert result[0]["stain_types"] == ""

    def test_stain_types_preserved(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[self._sample_row(stain_types="H&E;Ki-67;PD-L1")],
        )
        assert result[0]["stain_types"] == "H&E;Ki-67;PD-L1"

    # ------------------------------------------------------------------
    # Multiple rows
    # ------------------------------------------------------------------

    def test_multiple_rows_returned_in_order(self):
        rows = [
            self._sample_row(sample_id="P-001-T01", patient_id="P-001"),
            self._sample_row(sample_id="P-002-T01", patient_id="P-002"),
        ]
        result, _ = self._call(["P-001-T01", "P-002-T01"], rows=rows)
        assert [r["sample_id"] for r in result] == ["P-001-T01", "P-002-T01"]

    # ------------------------------------------------------------------
    # SQL generation
    # ------------------------------------------------------------------

    def test_sql_contains_sample_ids(self):
        _, mock_rq = self._call(["P-001-T01", "P-002-T01"], rows=[])
        sql_arg = mock_rq.call_args[0][0]
        params = mock_rq.call_args[0][2]
        assert ":sample_id_0" in sql_arg
        assert ":sample_id_1" in sql_arg
        assert [param.value for param in params] == ["P-001-T01", "P-002-T01"]

    def test_sql_queries_summary_table(self):
        from app.constants import SUMMARY_TABLE
        _, mock_rq = self._call(["P-001-T01"], rows=[])
        sql_arg = mock_rq.call_args[0][0]
        assert SUMMARY_TABLE in sql_arg

    def test_warehouse_id_forwarded_to_run_query(self):
        _, mock_rq = self._call(["P-001-T01"], rows=[])
        warehouse_arg = mock_rq.call_args[0][1]
        assert warehouse_arg == "wh-test"

    # ------------------------------------------------------------------
    # Null / missing column values
    # ------------------------------------------------------------------

    def test_null_servable_count_becomes_zero(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[self._sample_row(servable_slide_count=None)],
        )
        assert result[0]["servable_slide_count"] == 0

    def test_null_has_hne_becomes_zero(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[self._sample_row(has_hne=None)],
        )
        assert result[0]["has_hne"] == 0

    def test_null_has_ihc_becomes_zero(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[self._sample_row(has_ihc=None)],
        )
        assert result[0]["has_ihc"] == 0

    def test_null_non_servable_counts_become_zero(self):
        result, _ = self._call(
            ["P-001-T01"],
            rows=[
                self._sample_row(
                    non_servable_hne_slide_count=None,
                    non_servable_ihc_slide_count=None,
                )
            ],
        )
        assert result[0]["non_servable_hne_slide_count"] == 0
        assert result[0]["non_servable_ihc_slide_count"] == 0


def test_live_summary_uses_patient_slide_universe():
    with patch("app.meta._run_query", return_value=[]) as mock_rq:
        get_live_sample_slide_summary(["P-001-T01"], warehouse_id="wh-test")

    sql = mock_rq.call_args[0][0]
    assert "INNER JOIN patient_map p ON c.mrn = p.mrn" in sql
    assert "viewable_patient_summary AS" in sql
    assert f"FROM {meta_store._TABLE} d" in sql
    assert "INNER JOIN servable_inventory s ON d.image_id = s.image_id" in sql
    assert "non_viewable_patient_summary AS" in sql
    assert "GROUP BY d.patient_id" in sql
    assert "m.image_id" not in sql


class TestPatientAssociationRows:
    def test_prefers_canonical_association_table(self):
        with patch("app.meta_store.run_query", return_value=[{"image_id": "1"}]) as mock_rq:
            rows = meta_store.get_patient_association_rows("P-0001", "wh-test")

        assert rows == [{"image_id": "1"}]
        sql = mock_rq.call_args_list[0].args[0]
        assert meta_store._CANONICAL_ASSOCIATION_TABLE in sql

    def test_canonical_query_errors_propagate(self):
        with patch(
            "app.meta_store.run_query",
            side_effect=RuntimeError("Databricks query timed out"),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                meta_store.get_patient_association_rows("P-0001", "wh-test")

    def test_canonical_only_patient_still_builds_hierarchy(self):
        association_rows = [
            {
                "image_id": "42",
                "sample_id": None,
                "match_level": "UNMATCHED",
                "block_id": "specimen/2-B1",
                "block_label": "B1",
                "part_type": "Biopsy",
                "part_description": "Liver",
                "path_dx_title": "Liver biopsy",
                "stain_name": "H&E",
                "stain_group": "H&E (Initial)",
                "CANCER_TYPE": None,
                "CANCER_TYPE_DETAILED": None,
                "ONCOTREE_CODE": None,
                "PRIMARY_SITE": None,
                "SAMPLE_TYPE": None,
                "METASTATIC_SITE": None,
                "TUMOR_PURITY": None,
                "ONCOGENIC_MUTATIONS": None,
                "NUM_ONCOGENIC_MUTATIONS": None,
                "CVR_TMB_SCORE": None,
                "MSI_TYPE": None,
                "magnification": 20,
                "file_size_bytes": 1234,
                "slide_path": "s3://mskmind-bkt/reef-slides/42.svs",
                "reference_sample_id": "P-0001-T01-IM6",
                "reference_sequencing_date": "2024-01-20",
                "slide_timepoint_days": -12,
                "slide_timepoint_source": "Procedure date relative to tumor sequencing",
                "procedure_date": "2024-01-08",
            }
        ]

        with patch("app.meta._run_query", return_value=[]):
            with patch(
                "app.meta_store.get_patient_association_rows",
                return_value=association_rows,
            ):
                result = get_patient_hierarchy("P-0001", "wh-test")

        assert result is not None
        assert result["patient_id"] == "P-0001"
        assert result["samples"][0]["sample_id"] == "UNMATCHED"
        assert result["slide_associations"][0]["match_level"] == "UNMATCHED"
