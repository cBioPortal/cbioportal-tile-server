from unittest.mock import patch

import httpx

from app.oncokb import (
    ONCOKB_LICENSED_CNA_URL,
    ONCOKB_LICENSED_URL,
    ONCOKB_PUBLIC_CNA_URL,
    ONCOKB_PUBLIC_URL,
    OncoKbCopyNumberItem,
    OncoKbMutationItem,
    oncokb_annotate,
    oncokb_annotate_copy_number,
)


class _MockResponse:
    def __init__(self, payload, status_code=200, text="ok"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _MockAsyncClient:
    def __init__(self, recorder):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, headers=None, json=None):
        self._recorder.append({"url": url, "headers": headers, "json": json})
        return _MockResponse([{"ok": True}])


class TestOncoKbRoutes:
    async def test_mutation_route_uses_public_api_without_token(self):
        calls = []

        with (
            patch("app.oncokb.settings.oncokb_api_token", ""),
            patch(
                "app.oncokb.httpx.AsyncClient",
                side_effect=lambda timeout: _MockAsyncClient(calls),
            ),
        ):
            response = await oncokb_annotate(
                [OncoKbMutationItem(id="m1", alteration="V600E", gene={"entrezGeneId": 673})]
            )

        assert response == [{"ok": True}]
        assert calls[0]["url"] == ONCOKB_PUBLIC_URL
        assert "Authorization" not in calls[0]["headers"]

    async def test_copy_number_route_uses_licensed_api_with_token(self):
        calls = []

        with (
            patch("app.oncokb.settings.oncokb_api_token", "secret-token"),
            patch(
                "app.oncokb.httpx.AsyncClient",
                side_effect=lambda timeout: _MockAsyncClient(calls),
            ),
        ):
            response = await oncokb_annotate_copy_number(
                [
                    OncoKbCopyNumberItem(
                        id="c1",
                        copyNameAlterationType="AMPLIFICATION",
                        gene={"entrezGeneId": 673},
                    )
                ]
            )

        assert response == [{"ok": True}]
        assert calls[0]["url"] == ONCOKB_LICENSED_CNA_URL
        assert calls[0]["headers"]["Authorization"] == "Bearer secret-token"

    async def test_mutation_route_uses_licensed_api_with_token(self):
        calls = []

        with (
            patch("app.oncokb.settings.oncokb_api_token", "secret-token"),
            patch(
                "app.oncokb.httpx.AsyncClient",
                side_effect=lambda timeout: _MockAsyncClient(calls),
            ),
        ):
            await oncokb_annotate(
                [OncoKbMutationItem(id="m1", alteration="V600E", gene={"entrezGeneId": 673})]
            )

        assert calls[0]["url"] == ONCOKB_LICENSED_URL

    async def test_copy_number_route_uses_public_api_without_token(self):
        calls = []

        with (
            patch("app.oncokb.settings.oncokb_api_token", ""),
            patch(
                "app.oncokb.httpx.AsyncClient",
                side_effect=lambda timeout: _MockAsyncClient(calls),
            ),
        ):
            await oncokb_annotate_copy_number(
                [
                    OncoKbCopyNumberItem(
                        id="c1",
                        copyNameAlterationType="AMPLIFICATION",
                        gene={"entrezGeneId": 673},
                    )
                ]
            )

        assert calls[0]["url"] == ONCOKB_PUBLIC_CNA_URL

    async def test_oncokb_timeout_maps_to_504(self):
        class _TimeoutClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, headers=None, json=None):
                raise httpx.TimeoutException("timed out")

        with patch(
            "app.oncokb.httpx.AsyncClient",
            side_effect=lambda timeout: _TimeoutClient(),
        ):
            try:
                await oncokb_annotate(
                    [OncoKbMutationItem(id="m1", alteration="V600E", gene={"entrezGeneId": 673})]
                )
            except Exception as exc:
                assert exc.status_code == 504
            else:
                raise AssertionError("Expected timeout exception")
