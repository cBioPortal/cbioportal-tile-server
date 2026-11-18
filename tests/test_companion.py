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
    monkeypatch.setattr(companion.settings, "annotation_trusted_writers", ["loader@example.org"])
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


    def _machine_row(self, client, source: str = "precomputed") -> dict:
        body = annotation_body()
        body["body"]["provenance"] = {"source": source}
        response = client.post(
            "/annotations?studyId=study-a", json=body, headers=headers(user="loader@example.org")
        )
        assert response.status_code == 201
        return response.json()

    def test_only_a_trusted_writer_may_create_machine_rows_over_http(self, client):
        body = annotation_body()
        body["body"]["provenance"] = {"source": "agent"}
        assert client.post("/annotations?studyId=study-a", json=body, headers=headers()).status_code == 422
        batch = {"annotations": [annotation_body(), body]}
        assert (
            client.post("/annotations/batch?studyId=study-a", json=batch, headers=headers()).status_code
            == 422
        )
        listed = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert listed.json() == []
        assert (
            client.post(
                "/annotations?studyId=study-a", json=body, headers=headers(user="loader@example.org")
            ).status_code
            == 201
        )

    def test_put_by_the_creator_cannot_flip_source_to_machine(self, client):
        row = client.post("/annotations?studyId=study-a", json=annotation_body(), headers=headers()).json()
        update = {
            "version": 1,
            "body": {"label": "Tumor", "provenance": {"source": "precomputed"}},
        }
        response = client.put(f"/annotations/{row['id']}?studyId=study-a", json=update, headers=headers())
        assert response.status_code == 422
        update["body"]["provenance"] = {"source": "import"}
        response = client.put(f"/annotations/{row['id']}?studyId=study-a", json=update, headers=headers())
        assert response.status_code == 200
        assert response.json()["body"]["provenance"] == {"source": "import"}

    def test_any_user_of_the_study_may_review_a_machine_annotation(self, client):
        row = self._machine_row(client)
        bob = headers(user="bob@example.org")
        accepted = client.post(
            f"/wsi/annotations/{row['id']}/review?studyId=study-a",
            json={"decision": "accept", "version": 1},
            headers=bob,
        )
        assert accepted.status_code == 200
        assert accepted.json()["version"] == 2
        assert accepted.json()["created_by"] == "loader@example.org"
        assert accepted.json()["body"]["provenance"]["reviewed_by"] == "bob@example.org"

        rejected = client.post(
            f"/annotations/{row['id']}/review?studyId=study-a",
            json={"decision": "reject", "version": 2},
            headers=bob,
        )
        assert rejected.status_code == 200
        listed = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=bob
        )
        assert listed.json() == []
        audit = client.get(
            "/annotations",
            params={"studyId": "study-a", "slide_id": SLIDE_KEY, "include_rejected": "true"},
            headers=bob,
        )
        assert [item["id"] for item in audit.json()] == [row["id"]]
        # Review does not let bob edit or delete it.
        assert (
            client.put(f"/annotations/{row['id']}?studyId=study-a", json={"version": 3}, headers=bob).status_code
            == 403
        )
        assert client.delete(f"/annotations/{row['id']}?studyId=study-a", headers=bob).status_code == 403

    def test_review_is_refused_for_human_rows_other_studies_and_stale_versions(self, client):
        human = client.post("/annotations?studyId=study-a", json=annotation_body(), headers=headers()).json()
        bob = headers(user="bob@example.org")
        review = {"decision": "accept", "version": 1}
        assert (
            client.post(f"/annotations/{human['id']}/review?studyId=study-a", json=review, headers=bob).status_code
            == 403
        )
        row = self._machine_row(client, "agent")
        other_study = headers(user="bob@example.org", study="study-b")
        assert (
            client.post(
                f"/annotations/{row['id']}/review?studyId=study-b", json=review, headers=other_study
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/annotations/{row['id']}/review?studyId=study-a",
                json={"decision": "accept", "version": 9},
                headers=bob,
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/annotations/{row['id']}/review?studyId=study-a",
                json={"decision": "delete", "version": 1},
                headers=bob,
            ).status_code
            == 422
        )
        assert (
            client.post(f"/annotations/{row['id']}/review?studyId=study-a", json=review, headers=headers(user=None))
        ).status_code == 401

    def test_review_is_a_write_under_the_csrf_rules(self, client):
        row = self._machine_row(client)
        no_xhr = {k: v for k, v in headers().items() if k != "X-Requested-With"}
        review = {"decision": "accept", "version": 1}
        assert (
            client.post(f"/annotations/{row['id']}/review?studyId=study-a", json=review, headers=no_xhr).status_code
            == 403
        )
        assert (
            client.post(
                f"/annotations/{row['id']}/review?studyId=study-a",
                json=review,
                headers=headers(Origin="https://evil.example"),
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/annotations/{row['id']}/review?studyId=study-a",
                content='{"decision": "accept", "version": 1}',
                headers=headers(**{"Content-Type": "text/plain"}),
            ).status_code
            == 415
        )


# --- batches, comment threads and slide notes over HTTP ----------------------


NO_XHR = {"X-Auth-Request-User": "alice@example.org", "X-Auth-Request-Study": "study-a"}


class TestCollaborationRoutes:
    def _rows(self, client, count: int = 2, user: str = "alice@example.org") -> list[dict]:
        return [
            client.post(
                "/wsi/annotations?studyId=study-a",
                json=annotation_body(label=f"L{index}"),
                headers=headers(user=user),
            ).json()
            for index in range(count)
        ]

    def test_batch_update_over_http(self, client):
        rows = self._rows(client)
        response = client.post(
            "/wsi/annotations/batch-update?studyId=study-a",
            json={
                "items": [
                    {"id": rows[1]["id"], "version": 1, "body": {"label": "second"}},
                    {"id": rows[0]["id"], "version": 1, "visible_to": None},
                ]
            },
            headers=headers(),
        )
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [rows[1]["id"], rows[0]["id"]]
        assert response.json()[1]["visible_to"] is None
        assert response.headers["cache-control"] == "private, no-store"

    def test_batch_errors_name_the_item(self, client):
        rows = self._rows(client)
        too_many = client.post(
            "/annotations/batch-update?studyId=study-a",
            json={"items": [{"id": str(i), "version": 1} for i in range(101)]},
            headers=headers(),
        )
        assert too_many.status_code == 422
        assert too_many.json()["detail"][0]["loc"][:2] == ["body", "items"]
        empty = client.post("/annotations/batch-delete?studyId=study-a", json={"items": []}, headers=headers())
        assert empty.status_code == 422
        bad_item = client.post(
            "/annotations/batch-review?studyId=study-a",
            json={"items": [{"id": rows[0]["id"], "version": 1, "decision": "maybe"}]},
            headers=headers(),
        )
        assert bad_item.status_code == 422
        assert bad_item.json()["detail"][0]["loc"][:3] == ["body", "items", 0]

        stale = client.post(
            "/annotations/batch-update?studyId=study-a",
            json={"items": [{"id": rows[0]["id"], "version": 1}, {"id": rows[1]["id"], "version": 5}]},
            headers=headers(),
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["conflicts"] == [
            {
                "index": 1,
                "id": rows[1]["id"],
                "loc": ["body", "items", 1, "version"],
                "version": 5,
                "current_version": 1,
            }
        ]
        forbidden = client.post(
            "/annotations/batch-delete?studyId=study-a",
            json={"items": [{"id": rows[0]["id"], "version": 1}]},
            headers=headers(user="bob@example.org"),
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["index"] == 0
        missing = client.post(
            "/annotations/batch-delete?studyId=study-a",
            json={"items": [{"id": rows[0]["id"], "version": 1}, {"id": "nope", "version": 1}]},
            headers=headers(),
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["id"] == "nope"
        listed = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert len(listed.json()) == 2

        deleted = client.post(
            "/annotations/batch-delete?studyId=study-a",
            json={"items": [{"id": row["id"], "version": 1} for row in rows]},
            headers=headers(),
        )
        assert deleted.status_code == 200
        assert [row["id"] for row in deleted.json()] == [row["id"] for row in rows]

    def test_batch_review_over_http(self, client):
        machine = client.post(
            "/annotations?studyId=study-a",
            json={**annotation_body(), "body": {"label": "AI", "provenance": {"source": "model"}}},
            headers=headers(user="loader@example.org"),
        ).json()
        response = client.post(
            "/annotations/batch-review?studyId=study-a",
            json={"items": [{"id": machine["id"], "version": 1, "decision": "accept"}]},
            headers=headers(user="bob@example.org"),
        )
        assert response.status_code == 200
        assert response.json()[0]["body"]["provenance"]["reviewed_by"] == "bob@example.org"

    @pytest.mark.parametrize(
        "path", ["/annotations/batch-update", "/annotations/batch-delete", "/annotations/batch-review"]
    )
    def test_batches_follow_the_identity_and_csrf_rules(self, client, path):
        body = {"items": [{"id": "x", "version": 1, "decision": "accept"}]}
        assert client.post(f"{path}?studyId=study-a", json=body, headers=NO_XHR).status_code == 403
        assert client.post(f"{path}?studyId=study-b", json=body, headers=headers()).status_code == 403
        assert (
            client.post(f"{path}?studyId=study-a&studyId=study-a", json=body, headers=headers()).status_code
            == 403
        )
        assert client.post(f"{path}?studyId=study-a", json=body, headers=headers(user=None)).status_code == 401
        assert (
            client.post(
                f"{path}?studyId=study-a",
                content="{}",
                headers=headers(**{"Content-Type": "text/plain"}),
            ).status_code
            == 415
        )

    def test_comment_thread_over_http(self, client):
        row = self._rows(client, 1)[0]
        base = f"/wsi/annotations/{row['id']}/comments"
        bob = headers(user="bob@example.org")
        created = client.post(f"{base}?studyId=study-a", json={"body": "Agree"}, headers=bob)
        assert created.status_code == 201
        comment = created.json()
        assert comment["author"] == "bob@example.org"

        thread = client.get(base, params={"studyId": "study-a"}, headers=headers())
        assert [item["body"] for item in thread.json()] == ["Agree"]
        listed = client.get(
            "/wsi/annotations", params={"studyId": "study-a", "slide_id": SLIDE_KEY}, headers=headers()
        )
        assert listed.json()[0]["comment_count"] == 1

        other = client.put(f"{base}/{comment['id']}?studyId=study-a", json={"body": "x"}, headers=headers())
        assert other.status_code == 403
        edited = client.put(f"{base}/{comment['id']}?studyId=study-a", json={"body": "Agreed"}, headers=bob)
        assert edited.json()["body"] == "Agreed"
        too_long = client.post(f"{base}?studyId=study-a", json={"body": "x" * 4001}, headers=bob)
        assert too_long.status_code == 422

        assert client.post(f"{base}?studyId=study-a", json={"body": "x"}, headers=NO_XHR).status_code == 403
        assert (
            client.get(base, params={"studyId": "study-b"}, headers=headers(study="study-b")).status_code
            == 403
        )
        assert client.delete(f"{base}/{comment['id']}?studyId=study-a", headers=bob).status_code == 204
        assert client.get(base, params={"studyId": "study-a"}, headers=bob).json() == []

    def test_a_private_annotations_thread_is_hidden_from_others(self, client):
        private = client.post(
            "/annotations?studyId=study-a", json={**annotation_body(), "visible_to": None}, headers=headers()
        ).json()
        base = f"/annotations/{private['id']}/comments"
        bob = headers(user="bob@example.org")
        assert client.get(base, params={"studyId": "study-a"}, headers=bob).status_code == 404
        assert client.post(f"{base}?studyId=study-a", json={"body": "x"}, headers=bob).status_code == 404

    def test_slide_notes_over_http(self, client):
        note = {"slide_id": SLIDE_KEY, "study_id": "study-a", "text": "Margin", "tags": ["QC"], "visible_to": []}
        created = client.post("/wsi/slide-notes?studyId=study-a", json=note, headers=headers())
        assert created.status_code == 201
        row = created.json()
        assert row["created_by"] == "alice@example.org"

        listing = client.get(
            "/wsi/slide-notes",
            params={"studyId": "study-a", "slide_id": SLIDE_KEY},
            headers=headers(user="bob@example.org"),
        ).json()
        assert [item["id"] for item in listing["notes"]] == [row["id"]]
        assert listing["tags"] == [{"tag": "QC", "count": 1}]

        bob = headers(user="bob@example.org")
        assert (
            client.put(f"/slide-notes/{row['id']}?studyId=study-a", json={"version": 1, "text": "x"}, headers=bob)
        ).status_code == 403
        updated = client.put(
            f"/wsi/slide-notes/{row['id']}?studyId=study-a", json={"version": 1, "tags": ["QC", "LUAD"]}, headers=headers()
        )
        assert updated.json()["version"] == 2 and updated.json()["tags"] == ["QC", "LUAD"]
        assert (
            client.delete(f"/wsi/slide-notes/{row['id']}?studyId=study-a&version=1", headers=headers())
        ).status_code == 409
        assert (
            client.delete(f"/wsi/slide-notes/{row['id']}?studyId=study-a&version=2", headers=headers())
        ).status_code == 204

    def test_slide_notes_follow_the_identity_and_csrf_rules(self, client):
        note = {"slide_id": SLIDE_KEY, "study_id": "study-a", "text": "Margin"}
        assert client.post("/slide-notes?studyId=study-a", json=note, headers=NO_XHR).status_code == 403
        assert (
            client.post("/slide-notes?studyId=study-a", json={**note, "study_id": "study-b"}, headers=headers())
        ).status_code == 403
        assert client.post("/slide-notes?studyId=study-b", json=note, headers=headers()).status_code == 403
        assert client.post("/slide-notes", json=note, headers=headers()).status_code == 403
        assert (
            client.get("/slide-notes", params={"studyId": "study-a", "slide_id": "470775"}, headers=headers())
        ).status_code == 422
        assert (
            client.post("/slide-notes?studyId=study-a", json={**note, "tags": ["bad;tag"]}, headers=headers())
        ).status_code == 422
        assert client.get("/slide-notes", params={"slide_id": SLIDE_KEY}, headers=headers()).status_code == 403


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
    assert not any(
        path.startswith(("/annotations", "/slide-notes", "/agent", "/research")) for path in paths
    )


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
