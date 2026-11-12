"""Assistant and research routes in the companion app, with proxy headers."""

import base64

import pytest
from fastapi.testclient import TestClient

from app import agent, annotations, companion, research

SLIDE_A = "a" * 12
XHR = {"X-Requested-With": "XMLHttpRequest"}


def headers(user: str | None = "alice@example.org", study: str = "study-a") -> dict:
    result = {**XHR, "X-Auth-Request-Study": study}
    if user is not None:
        result["X-Auth-Request-User"] = user
    return result


def context(study: str = "study-a") -> dict:
    return {
        "study_id": study,
        "patient_id": "patient-a",
        "slide_id": SLIDE_A,
        "viewport": {
            "slide_width": 1000,
            "slide_height": 800,
            "image_data_url": "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode(),
            "image_width": 100,
            "image_height": 80,
            "image_transform": [1, 0, 0, 0, 1, 0],
            "source_fingerprint": "source-v2-fingerprint",
            "capture_id": "capture-1",
            "viewer_generation": 3,
        },
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "companion.db")
    monkeypatch.setattr(companion.settings, "annotation_database_url", "")
    monkeypatch.setattr(companion.settings, "annotation_db_path", db_path)
    monkeypatch.setattr(companion.settings, "annotation_allow_anonymous", False)
    monkeypatch.setattr(companion.settings, "companion_allowed_origins", [])
    monkeypatch.setattr(companion.settings, "companion_max_body_bytes", 4 * 1024 * 1024)
    monkeypatch.setattr(companion.settings, "agent_enabled", False)
    monkeypatch.setattr(companion.settings, "research_enabled", False)
    agent._rate_windows.clear()
    yield companion.settings, db_path
    annotations._db_path = ""
    annotations._db_url = ""


@pytest.fixture
def client(env):
    with TestClient(companion.app) as test_client:
        yield test_client


async def _proposal(db_path: str, user_sub: str = "alice@example.org") -> str:
    await agent.init_db(db_path=db_path, db_url="")
    await annotations.init_db(db_path=db_path, db_url="")
    run_context = agent.AgentRunContext(
        user_sub=user_sub,
        session_id="session-a",
        context=agent.AgentContext(**context()),
    )
    result = await agent._bedrock_tool(
        "wsi_propose_annotations",
        {
            "geometry_type": "rectangle",
            "points": [{"x": 100, "y": 100}, {"x": 300, "y": 300}],
            "label": "candidate",
            "layer_name": "Tumor",
            "color": "#ef4444",
            "confidence": 0.8,
            "rationale": "The captured viewport contains a candidate.",
        },
        run_context,
    )
    return result["proposal_id"]


COMMIT = {"source_fingerprint": "source-v2-fingerprint", "viewer_generation": 3, "slide_id": SLIDE_A}


class TestDisabledByDefault:
    def test_assistant_routes_are_404_when_disabled(self, client):
        chat = client.post(
            "/agent/chat?studyId=study-a",
            json={"session_id": "s", "message": "hi", "context": context()},
            headers=headers(),
        )
        assert chat.status_code == 404
        actions = client.get(
            "/agent/actions", params={"studyId": "study-a", "session_id": "s"}, headers=headers()
        )
        assert actions.status_code == 404
        for verb in ("apply", "reject", "complete", "commit-annotations"):
            response = client.post(f"/agent/actions/x/{verb}?studyId=study-a", json={}, headers=headers())
            assert response.status_code == 404, verb

    def test_research_routes_are_404_when_disabled(self, client):
        response = client.get(
            f"/research/v1/slides/study-a/{SLIDE_A}/models?studyId=study-a", headers=headers()
        )
        assert response.status_code == 404

    def test_health_routes_need_no_identity(self, client):
        assert client.get("/agent/health").json()["enabled"] is False
        assert client.get("/research/v1/health").status_code == 200
        ready = client.get("/ready").json()
        assert ready["agent_enabled"] is False
        assert ready["research_enabled"] is False


class TestEnabledAssistant:
    @pytest.fixture(autouse=True)
    def enabled(self, env, monkeypatch):
        settings, _ = env
        monkeypatch.setattr(settings, "agent_enabled", True)
        monkeypatch.setattr(settings, "research_enabled", True)
        monkeypatch.setattr(agent, "_provider_configured", lambda **_: True)

    def test_requests_without_identity_are_401(self, client):
        response = client.get(
            "/agent/actions", params={"studyId": "study-a", "session_id": "s"}, headers=XHR
        )
        assert response.status_code == 401
        response = client.get(
            "/agent/actions",
            params={"studyId": "study-a", "session_id": "s"},
            headers=headers(user=None),
        )
        assert response.status_code == 401

    def test_chat_context_study_must_match(self, client):
        response = client.post(
            "/agent/chat?studyId=study-a",
            json={"session_id": "s", "message": "hi", "context": context("study-b")},
            headers=headers(),
        )
        assert response.status_code == 403

    def test_chat_needs_the_csrf_header(self, client):
        response = client.post(
            "/agent/chat?studyId=study-a",
            json={"session_id": "s", "message": "hi", "context": context()},
            headers={k: v for k, v in headers().items() if k != "X-Requested-With"},
        )
        assert response.status_code == 403

    def test_chat_accepts_a_viewport_sized_body(self, client, monkeypatch):
        async def fake_stream(request, user_sub):
            yield agent._sse("done", {"user": user_sub})

        monkeypatch.setattr(agent, "_stream_agent", fake_stream)
        monkeypatch.setattr(agent, "_validate_context", lambda ctx: None)
        large = context()
        large["viewport"]["image_data_url"] = "data:image/jpeg;base64," + "A" * 2_700_000
        response = client.post(
            "/agent/chat?studyId=study-a",
            json={"session_id": "s", "message": "hi", "context": large},
            headers=headers(),
        )
        assert response.status_code == 200
        assert "alice@example.org" in response.text

    @pytest.mark.asyncio
    async def test_commit_writes_annotations_as_the_forwarded_user(self, client, env):
        _, db_path = env
        proposal_id = await _proposal(db_path)

        listed = client.get(
            "/agent/actions", params={"studyId": "study-a", "session_id": "session-a"}, headers=headers()
        )
        assert [item["id"] for item in listed.json()] == [proposal_id]

        committed = client.post(
            f"/agent/actions/{proposal_id}/commit-annotations?studyId=study-a",
            json=COMMIT,
            headers=headers(),
        )
        assert committed.status_code == 200, committed.text
        assert [row["created_by"] for row in committed.json()["annotations"]] == ["alice@example.org"]

        rows = client.get(
            "/annotations", params={"studyId": "study-a", "slide_id": SLIDE_A}, headers=headers()
        )
        assert [row["created_by"] for row in rows.json()] == ["alice@example.org"]

    @pytest.mark.asyncio
    async def test_a_proposal_is_bound_to_its_user_and_study(self, client, env):
        _, db_path = env
        proposal_id = await _proposal(db_path)

        other_study = client.post(
            f"/agent/actions/{proposal_id}/commit-annotations?studyId=study-b",
            json=COMMIT,
            headers=headers(study="study-b"),
        )
        assert other_study.status_code == 403
        other_user = client.post(
            f"/agent/actions/{proposal_id}/commit-annotations?studyId=study-a",
            json=COMMIT,
            headers=headers(user="bob@example.org"),
        )
        assert other_user.status_code == 404
        reject_elsewhere = client.post(
            f"/agent/actions/{proposal_id}/reject?studyId=study-b", headers=headers(study="study-b")
        )
        assert reject_elsewhere.status_code == 403
        listed_elsewhere = client.get(
            "/agent/actions",
            params={"studyId": "study-b", "session_id": "session-a"},
            headers=headers(study="study-b"),
        )
        assert listed_elsewhere.json() == []

    def test_research_path_study_must_match(self, client):
        response = client.get(
            f"/research/v1/slides/study-b/{SLIDE_A}/models?studyId=study-a", headers=headers()
        )
        assert response.status_code == 403

    def test_research_mcp_arguments_study_must_match(self, client, monkeypatch):
        research._rate_windows.clear()
        response = client.post(
            "/research/v1/mcp?studyId=study-a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "wsi_region_search",
                    "arguments": {"study_id": "study-b", "slide_id": SLIDE_A, "model": "m"},
                },
            },
            headers=headers(),
        )
        assert response.status_code == 403


def test_the_pixel_app_serves_no_assistant_or_research_routes():
    from app import main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert not any(path.startswith(("/annotations", "/agent", "/research")) for path in paths)
    companion_paths = {getattr(route, "path", "") for route in companion.app.routes}
    assert "/agent/chat" in companion_paths
    assert "/research/v1/mcp" in companion_paths
