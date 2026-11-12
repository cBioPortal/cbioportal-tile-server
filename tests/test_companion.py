"""The companion service: proxy-asserted identity, CSRF guard and routing."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import annotations, companion, request_identity
from app.request_identity import require_same_study, trusted_identity

SLIDE_KEY = "0123456789ab"
XHR = {"X-Requested-With": "XMLHttpRequest"}


def headers(user: str | None = "alice@example.org", study: str | None = "study-a", **extra) -> dict:
    result = dict(XHR)
    if user is not None:
        result["X-Auth-Request-User"] = user
    if study is not None:
        result["X-Auth-Request-Study"] = study
    result.update(extra)
    return result


def annotation_body(study: str = "study-a", label: str = "Tumor") -> dict:
    return {
        "slide_id": SLIDE_KEY,
        "study_id": study,
        "body": {"label": label, "comment": "", "type": "Polygon"},
        "target": {
            "selector": {"type": "SvgSelector", "value": "<svg><polygon points='1,1 4,1 4,4'/></svg>"}
        },
        "visible_to": [],
    }


def make_request(query: str = "studyId=study-a", **header_values: str) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in header_values.items()]
    return Request(
        {"type": "http", "method": "GET", "path": "/annotations", "query_string": query.encode(), "headers": raw}
    )


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    monkeypatch.setattr(companion.settings, "annotation_database_url", "")
    monkeypatch.setattr(companion.settings, "annotation_db_path", str(tmp_path / "annotations.db"))
    monkeypatch.setattr(companion.settings, "annotation_allow_anonymous", False)
    monkeypatch.setattr(companion.settings, "annotation_anonymous_user", "anonymous")
    monkeypatch.setattr(companion.settings, "companion_allowed_origins", [])
    monkeypatch.setattr(companion.settings, "companion_max_body_bytes", 4 * 1024 * 1024)
    yield companion.settings
    annotations._db_path = ""
    annotations._db_url = ""


@pytest.fixture
def client(settings_env):
    with TestClient(companion.app) as test_client:
        yield test_client


# --- trusted_identity -------------------------------------------------------


class TestTrustedIdentity:
    def test_headers_give_the_user_and_study(self, settings_env):
        identity = trusted_identity(
            make_request(**{"X-Auth-Request-User": "alice", "X-Auth-Request-Study": "study-a"})
        )
        assert identity == {"sub": "alice", "groups": [], "study_id": "study-a"}

    def test_missing_study_header_is_401(self, settings_env):
        with pytest.raises(HTTPException) as exc:
            trusted_identity(make_request(**{"X-Auth-Request-User": "alice"}))
        assert exc.value.status_code == 401

    def test_malformed_study_header_is_401(self, settings_env):
        with pytest.raises(HTTPException) as exc:
            trusted_identity(
                make_request(**{"X-Auth-Request-User": "alice", "X-Auth-Request-Study": "../x"})
            )
        assert exc.value.status_code == 401

    @pytest.mark.parametrize(
        "query",
        ["", "studyId=study-b", "studyId=study-a&studyId=study-a", "studyId=study-a&StudyID=study-b", "study_id=study-a"],
    )
    def test_studyid_must_be_given_once_and_match(self, settings_env, query):
        with pytest.raises(HTTPException) as exc:
            trusted_identity(
                make_request(query, **{"X-Auth-Request-User": "alice", "X-Auth-Request-Study": "study-a"})
            )
        assert exc.value.status_code == 403

    def test_missing_user_is_401_unless_anonymous_is_allowed(self, settings_env, monkeypatch):
        request = make_request(**{"X-Auth-Request-Study": "study-a"})
        with pytest.raises(HTTPException) as exc:
            trusted_identity(request)
        assert exc.value.status_code == 401

        monkeypatch.setattr(settings_env, "annotation_allow_anonymous", True)
        monkeypatch.setattr(settings_env, "annotation_anonymous_user", "local-development")
        assert trusted_identity(request)["sub"] == "local-development"

    def test_anonymous_never_replaces_a_forwarded_user(self, settings_env, monkeypatch):
        monkeypatch.setattr(settings_env, "annotation_allow_anonymous", True)
        identity = trusted_identity(
            make_request(**{"X-Auth-Request-User": "alice", "X-Auth-Request-Study": "study-a"})
        )
        assert identity["sub"] == "alice"

    def test_a_user_with_control_characters_is_refused(self, settings_env):
        with pytest.raises(HTTPException) as exc:
            trusted_identity(
                make_request(**{"X-Auth-Request-User": "al\x01ice", "X-Auth-Request-Study": "study-a"})
            )
        assert exc.value.status_code == 401

    def test_the_study_is_never_none(self, settings_env):
        user = {"sub": "alice", "groups": [], "study_id": "study-a"}
        require_same_study(user, "study-a")
        for other in ("study-b", None, ""):
            with pytest.raises(HTTPException) as exc:
                require_same_study(user, other)
            assert exc.value.status_code == 403

    def test_no_token_settings_remain(self):
        for name in ("wsi_auth_secret", "wsi_auth_audience", "annotation_auth_enabled"):
            assert not hasattr(companion.settings, name)


# --- annotation routes over HTTP -------------------------------------------


class TestAnnotationRoutes:
    def test_crud_records_the_forwarded_user(self, client):
        created = client.post("/annotations?studyId=study-a", json=annotation_body(), headers=headers())
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["created_by"] == "alice@example.org"
        assert row["study_id"] == "study-a"
        assert created.headers["cache-control"] == "private, no-store"

        listed = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [row["id"]]

        updated = client.put(
            f"/annotations/{row['id']}?studyId=study-a",
            json={"body": {"label": "Stroma", "comment": "", "type": "Polygon"}, "version": 1},
            headers=headers(),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2
        assert updated.json()["created_by"] == "alice@example.org"

        deleted = client.delete(f"/annotations/{row['id']}?studyId=study-a", headers=headers())
        assert deleted.status_code == 204
        listed = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert listed.json() == []

    def test_wsi_prefix_is_accepted(self, client):
        response = client.get(
            "/wsi/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert response.status_code == 200

    def test_list_only_returns_the_authorized_studys_rows(self, client):
        client.post("/annotations?studyId=study-a", json=annotation_body("study-a"), headers=headers())
        response = client.get(
            "/annotations",
            params={"studyId": "study-b", "slide_id": SLIDE_KEY},
            headers=headers(study="study-b"),
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_requests_without_proxy_headers_are_refused(self, client):
        params = {"studyId": "study-a", "slide_id": SLIDE_KEY}
        assert client.get("/annotations", params=params, headers=XHR).status_code == 401
        assert client.get("/annotations", params=params, headers=headers(user=None)).status_code == 401

    def test_anonymous_writes_use_the_configured_identity(self, client, settings_env, monkeypatch):
        monkeypatch.setattr(settings_env, "annotation_allow_anonymous", True)
        monkeypatch.setattr(settings_env, "annotation_anonymous_user", "local-development")
        created = client.post(
            "/annotations?studyId=study-a", json=annotation_body(), headers=headers(user=None)
        )
        assert created.status_code == 201
        assert created.json()["created_by"] == "local-development"

    def test_body_study_must_match_the_authorized_study(self, client):
        response = client.post(
            "/annotations?studyId=study-a", json=annotation_body("study-b"), headers=headers()
        )
        assert response.status_code == 403

    def test_studyid_must_match_the_authorized_study(self, client):
        response = client.post(
            "/annotations?studyId=study-b", json=annotation_body("study-a"), headers=headers()
        )
        assert response.status_code == 403
        response = client.post("/annotations", json=annotation_body("study-a"), headers=headers())
        assert response.status_code == 403

    def test_a_forged_header_cannot_reach_another_studys_row(self, client):
        row = client.post(
            "/annotations?studyId=study-a", json=annotation_body(), headers=headers()
        ).json()
        # Authorized for study-b (the user's own study), aiming at study-a's row.
        forged = headers(study="study-b")
        update = client.put(
            f"/annotations/{row['id']}?studyId=study-b", json={"version": 1}, headers=forged
        )
        assert update.status_code == 403
        delete = client.delete(f"/annotations/{row['id']}?studyId=study-b", headers=forged)
        assert delete.status_code == 403
        # Naming study-a in the query while authorized for study-b is refused too.
        delete = client.delete(f"/annotations/{row['id']}?studyId=study-a", headers=forged)
        assert delete.status_code == 403
        still_there = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert [item["id"] for item in still_there.json()] == [row["id"]]

    def test_only_the_creator_may_change_a_row(self, client):
        row = client.post(
            "/annotations?studyId=study-a", json=annotation_body(), headers=headers()
        ).json()
        other = headers(user="bob@example.org")
        assert (
            client.put(f"/annotations/{row['id']}?studyId=study-a", json={"version": 1}, headers=other).status_code
            == 403
        )
        assert client.delete(f"/annotations/{row['id']}?studyId=study-a", headers=other).status_code == 403


# --- CSRF guard ---------------------------------------------------------------


class TestCsrfGuard:
    def test_unsafe_requests_need_x_requested_with(self, client):
        no_xhr = {k: v for k, v in headers().items() if k != "X-Requested-With"}
        response = client.post("/annotations?studyId=study-a", json=annotation_body(), headers=no_xhr)
        assert response.status_code == 403
        wrong = headers(**{"X-Requested-With": "fetch"})
        response = client.post("/annotations?studyId=study-a", json=annotation_body(), headers=wrong)
        assert response.status_code == 403
        response = client.delete("/annotations/x?studyId=study-a", headers=no_xhr)
        assert response.status_code == 403

    def test_reads_do_not_need_x_requested_with(self, client):
        no_xhr = {k: v for k, v in headers().items() if k != "X-Requested-With"}
        response = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=no_xhr
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"])
    def test_a_body_must_be_json(self, client, content_type):
        response = client.post(
            "/annotations?studyId=study-a",
            content=b'{"slide_id": "0123456789ab"}',
            headers=headers(**{"Content-Type": content_type}),
        )
        assert response.status_code == 415

    def test_json_with_charset_is_accepted(self, client):
        import json

        response = client.post(
            "/annotations?studyId=study-a",
            content=json.dumps(annotation_body()).encode(),
            headers=headers(**{"Content-Type": "application/json; charset=utf-8"}),
        )
        assert response.status_code == 201

    @pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://testserver.evil.example"])
    def test_a_foreign_origin_is_refused(self, client, origin):
        response = client.post(
            "/annotations?studyId=study-a", json=annotation_body(), headers=headers(Origin=origin)
        )
        assert response.status_code == 403

    def test_cross_site_fetch_metadata_is_refused(self, client):
        response = client.post(
            "/annotations?studyId=study-a",
            json=annotation_body(),
            headers=headers(**{"Sec-Fetch-Site": "cross-site"}),
        )
        assert response.status_code == 403

    def test_same_host_and_configured_origins_are_accepted(self, client, settings_env, monkeypatch):
        same = client.post(
            "/annotations?studyId=study-a", json=annotation_body(), headers=headers(Origin="http://testserver")
        )
        assert same.status_code == 201
        monkeypatch.setattr(settings_env, "companion_allowed_origins", ["https://portal.example.org/"])
        listed = client.post(
            "/annotations?studyId=study-a",
            json=annotation_body(),
            headers=headers(Origin="https://portal.example.org"),
        )
        assert listed.status_code == 201

    def test_no_cors_headers_are_sent(self, client):
        preflight = client.options(
            "/annotations?studyId=study-a",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-requested-with, content-type",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers
        response = client.get(
            "/annotations",
            params={"studyId": "study-a", "slide_id": SLIDE_KEY},
            headers=headers(Origin="https://evil.example"),
        )
        assert "access-control-allow-origin" not in response.headers


# --- request size -------------------------------------------------------------


class TestBodyLimit:
    def test_declared_oversized_body_is_413(self, client, settings_env, monkeypatch):
        monkeypatch.setattr(settings_env, "companion_max_body_bytes", 1024)
        body = annotation_body()
        body["body"]["comment"] = "x" * 2048
        response = client.post("/annotations?studyId=study-a", json=body, headers=headers())
        assert response.status_code == 413

    def test_streamed_oversized_body_is_413(self, client, settings_env, monkeypatch):
        monkeypatch.setattr(settings_env, "companion_max_body_bytes", 1024)

        def chunks():
            yield b'{"comment": "'
            for _ in range(8):
                yield b"x" * 256
            yield b'"}'

        response = client.post(
            "/annotations?studyId=study-a",
            content=chunks(),
            headers=headers(**{"Content-Type": "application/json"}),
        )
        assert response.status_code == 413

    def test_default_limit_fits_a_viewport_image(self):
        assert companion.settings.companion_max_body_bytes >= 3 * 1024 * 1024


# --- health and separation from the pixel app --------------------------------


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["annotation_storage"] == "sqlite"
    assert ready.json()["anonymous_allowed"] is False


def _paths(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_the_companion_serves_no_pixels():
    paths = _paths(companion.app)
    assert not any(path.startswith(("/tiles", "/thumbnails")) for path in paths)
    assert "/annotations" in paths


def test_the_pixel_app_serves_no_companion_routes():
    from app import main

    paths = _paths(main.app)
    assert not any(path.startswith(("/annotations", "/agent", "/research")) for path in paths)


def test_the_pixel_app_does_not_answer_annotation_requests():
    from app import main

    client = TestClient(main.app)
    response = client.get(
        "/wsi/annotations",
        params={"studyId": "study-a", "slide_id": SLIDE_KEY},
        headers=headers(),
    )
    assert response.status_code == 404


def test_request_identity_reads_only_the_documented_headers():
    assert request_identity.USER_HEADER == "X-Auth-Request-User"
    assert request_identity.STUDY_HEADER == "X-Auth-Request-Study"
