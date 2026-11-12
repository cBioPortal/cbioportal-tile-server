from app.query_planner import build_query_plan


def test_query_plan_expands_pathology_terms_and_preserves_original_query():
    plan = build_query_plan("invasive carcinoma with tumor infiltrating lymphocytes")

    assert plan["primary"] == "invasive carcinoma"
    assert "invasive carcinoma" in plan["positive"]
    assert any("adenocarcinoma" in value for value in plan["positive"])
    assert "tumor infiltrating lymphocytes" in plan["positive"]
    assert plan["negative"] == []


def test_query_plan_extracts_exclusions():
    plan = build_query_plan("malignant glands excluding necrosis and adipose")

    assert plan["primary"] == "malignant glands"
    assert plan["negative"] == ["necrosis", "adipose"]
    assert "Searching for malignant glands" in plan["summary"]
