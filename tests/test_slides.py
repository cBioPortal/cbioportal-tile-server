"""Tests for slide location routing logic in app.slide_store."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.slide_store import open_slide, resolve_s3_location


def _settings(
    aws_endpoint_url="http://ecs:9020",
    aws_access_key_id="key",
    aws_secret_access_key="secret",
    blockcache_path="",
):
    m = MagicMock()
    m.aws_endpoint_url      = aws_endpoint_url
    m.aws_access_key_id     = aws_access_key_id
    m.aws_secret_access_key = aws_secret_access_key
    m.blockcache_path       = blockcache_path
    return m


def resolve(slide_id, **kwargs):
    with patch("app.slide_store.settings", _settings(**kwargs)):
        return resolve_s3_location(slide_id)


class TestValidS3Uris:
    def test_bucket_extracted(self):
        bucket, _, _ = resolve("s3://mskmind-bkt/reef-slides/3735444.svs")
        assert bucket == "mskmind-bkt"

    def test_key_extracted(self):
        _, key, _ = resolve("s3://mskmind-bkt/reef-slides/3735444.svs")
        assert key == "reef-slides/3735444.svs"

    def test_nested_key(self):
        _, key, _ = resolve("s3://bucket/a/b/c/slide.svs")
        assert key == "a/b/c/slide.svs"

    def test_opts_include_endpoint(self):
        _, _, opts = resolve("s3://bucket/key.svs", aws_endpoint_url="http://ecs:9020")
        assert opts["endpoint_url"] == "http://ecs:9020"

    def test_opts_include_credentials(self):
        _, _, opts = resolve("s3://bucket/key.svs", aws_access_key_id="k", aws_secret_access_key="s")
        assert opts["key"] == "k"
        assert opts["secret"] == "s"

    def test_opts_omit_empty_endpoint(self):
        _, _, opts = resolve("s3://bucket/key.svs", aws_endpoint_url="")
        assert "endpoint_url" not in opts

    def test_opts_omit_empty_credentials(self):
        _, _, opts = resolve("s3://bucket/key.svs", aws_access_key_id="", aws_secret_access_key="")
        assert "key" not in opts
        assert "secret" not in opts


class TestInvalidSlideIds:
    def test_non_s3_uri_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve("3735444")

    def test_http_uri_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve("http://example.com/slide.svs")

    def test_bare_name_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve("TCGA-HW-7489.svs")

    def test_s3_no_key_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve("s3://bucket-only")


class TestLocalSlidePaths:
    def test_absolute_path_opens_without_s3(self, tmp_path):
        slide_path = tmp_path / "slide.svs"
        slide_path.write_bytes(b"fake")
        with patch("app.slide_store.TiffSlide", return_value=MagicMock()) as mock_slide:
            slide, fileobj = open_slide(str(slide_path), logger=MagicMock())
        assert slide is mock_slide.return_value
        assert fileobj is None

    def test_file_uri_opens_without_s3(self, tmp_path):
        slide_path = tmp_path / "slide.svs"
        slide_path.write_bytes(b"fake")
        with patch("app.slide_store.TiffSlide", return_value=MagicMock()) as mock_slide:
            slide, fileobj = open_slide(f"file://{slide_path}", logger=MagicMock())
        assert slide is mock_slide.return_value
        assert fileobj is None

    def test_missing_local_path_raises(self):
        missing = Path("/tmp/definitely-missing-slide.svs")
        with pytest.raises(FileNotFoundError):
            open_slide(str(missing), logger=MagicMock())
