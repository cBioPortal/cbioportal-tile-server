import pytest

from app import meta_store


def test_external_result_requires_exact_https_allowlist(monkeypatch):
    monkeypatch.setattr(
        meta_store.settings,
        "databricks_external_result_allowed_hosts",
        ["results.example.org"],
    )

    assert (
        meta_store._validated_external_link("https://results.example.org/chunk")
        == "https://results.example.org/chunk"
    )
    with pytest.raises(RuntimeError):
        meta_store._validated_external_link("http://results.example.org/chunk")
    with pytest.raises(RuntimeError):
        meta_store._validated_external_link("https://127.0.0.1/chunk")
    with pytest.raises(RuntimeError):
        meta_store._validated_external_link("https://child.results.example.org/chunk")


def test_external_result_headers_drop_credential_headers():
    headers = meta_store._safe_external_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "x-ms-version": "2020-01-01",
            "Accept": "application/json",
        }
    )

    assert headers == {
        "x-ms-version": "2020-01-01",
        "Accept": "application/json",
    }
