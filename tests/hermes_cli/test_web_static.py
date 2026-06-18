"""Tests for the dashboard static-file gzip/cache behaviour.

Covers:
- Accept-Encoding parser compliance (RFC 7231 §5.3.4)
- CSS endpoint gzip + prefix rewriting + Vary
- Hashed asset gzip via _OptimizedStaticFiles
"""

import gzip
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hermes_cli.web_server import (
    _accepts_gzip_static,
    _GZIP_COMPRESS_LEVEL,
    mount_spa,
)


# ---------------------------------------------------------------------------
# _accepts_gzip_static parser
# ---------------------------------------------------------------------------


class TestAcceptsGzipStatic:
    """Unit tests for the shared Accept-Encoding parser."""

    @pytest.mark.parametrize(
        "header, expected",
        [
            ("", False),
            ("gzip", True),
            ("x-gzip", True),
            ("GZIP", True),
            ("X-GZip", True),
            ("deflate", False),
            ("gzip;q=0", False),
            ("gzip;q=0.0", False),
            ("gzip;q=0.5", True),
            ("gzip;q=1", True),
            ("gzip;q=1.0", True),
            ("gzip; q=0", False),
            ("deflate, gzip;q=0.5", True),
            ("*", True),
            ("*;q=0", False),
            ("*;q=1", True),
            ("*;q=0.5", True),
            # Explicit gzip always overrides wildcard, regardless of order.
            ("*;q=0, gzip;q=1", True),
            ("gzip;q=0, *;q=1", False),
            # Malformed q-values reject the encoding.
            ("gzip;q=abc", False),
            ("gzip;q", False),
            ("gzip;q=", False),
            # Other encodings with wildcard but no explicit gzip follow wildcard q.
            ("*;q=0, deflate", False),
            ("deflate, *;q=0", False),
            # Multi-parameter tokens.
            ("gzip;q=0.5;ext=foo", True),
            ("gzip;ext=foo;q=0", False),
        ],
    )
    def test_parser(self, header, expected):
        assert _accepts_gzip_static(header) is expected


# ---------------------------------------------------------------------------
# mount_spa static endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def static_app(tmp_path, monkeypatch):
    """Build a FastAPI app with mount_spa pointed at a temp WEB_DIST."""
    web_dist = tmp_path / "web_dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<!doctype html><html></html>")
    assets = web_dist / "assets"
    assets.mkdir()

    monkeypatch.setattr("hermes_cli.web_server.WEB_DIST", web_dist)

    app = FastAPI()
    mount_spa(app)
    return app, web_dist


class TestServeCss:
    """Tests for /assets/{filename}.css endpoint."""

    def test_gzip_response_when_accepted(self, static_app):
        app, web_dist = static_app
        css = "body { background: red; }\n" + "/* padding */\n" * 50
        (web_dist / "assets" / "theme.css").write_text(css)

        client = TestClient(app)
        resp = client.get("/assets/theme.css", headers={"accept-encoding": "gzip"})

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert resp.headers["content-type"].startswith("text/css")
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "accept-encoding" in resp.headers["vary"]
        assert "x-forwarded-prefix" in resp.headers["vary"]
        assert int(resp.headers["content-length"]) < len(css.encode("utf-8"))
        assert resp.text == css

    def test_uncompressed_response_without_accept_encoding(self, static_app):
        app, web_dist = static_app
        css = "body { color: blue; }"
        (web_dist / "assets" / "theme.css").write_text(css)

        client = TestClient(app)
        resp = client.get("/assets/theme.css")

        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
        assert resp.text == css
        assert int(resp.headers["content-length"]) == len(css.encode("utf-8"))
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_head_returns_no_body_with_content_length(self, static_app):
        app, web_dist = static_app
        css = "body { color: blue; }\n" + "/* pad */\n" * 200
        (web_dist / "assets" / "theme.css").write_text(css)

        client = TestClient(app)
        resp = client.head("/assets/theme.css", headers={"accept-encoding": "gzip"})

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert int(resp.headers["content-length"]) > 0
        assert resp.content == b""

    def test_prefix_rewrites_absolute_urls(self, static_app):
        app, web_dist = static_app
        css = "@font-face { src: url(/fonts/foo.woff2); }\n" + "/* pad */\n" * 200
        (web_dist / "assets" / "theme.css").write_text(css)

        client = TestClient(app)
        resp = client.get(
            "/assets/theme.css",
            headers={
                "x-forwarded-prefix": "/hermes",
                "accept-encoding": "gzip",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert "url(/hermes/fonts/foo.woff2)" in resp.text
        assert "x-forwarded-prefix" in resp.headers["vary"]

    def test_404_for_missing_css(self, static_app):
        app, _ = static_app
        client = TestClient(app)
        resp = client.get("/assets/missing.css")
        assert resp.status_code == 404


class TestOptimizedStaticFiles:
    """Tests for _OptimizedStaticFiles hashed asset serving."""

    def _make_large_js(self, assets_dir: Path, name: str) -> bytes:
        """Create a JS file large enough to trigger the >1024 byte gzip threshold."""
        content = f"console.log('{name}');\n" + "// padding\n" * 200
        data = content.encode("utf-8")
        assets_dir.joinpath(name).write_bytes(data)
        return data

    def test_gzip_for_hashed_js(self, static_app):
        app, web_dist = static_app
        original = self._make_large_js(web_dist / "assets", "index-abc123.js")

        client = TestClient(app)
        resp = client.get("/assets/index-abc123.js", headers={"accept-encoding": "gzip"})

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert resp.headers["content-type"].startswith("text/javascript")
        assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "accept-encoding" in resp.headers["vary"]
        assert resp.content == original

    def test_head_hashed_js(self, static_app):
        app, web_dist = static_app
        self._make_large_js(web_dist / "assets", "index-abc123.js")

        client = TestClient(app)
        resp = client.head("/assets/index-abc123.js", headers={"accept-encoding": "gzip"})

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert int(resp.headers["content-length"]) > 0
        assert resp.content == b""

    def test_no_gzip_when_client_rejects(self, static_app):
        app, web_dist = static_app
        original = self._make_large_js(web_dist / "assets", "index-abc123.js")

        client = TestClient(app)
        resp = client.get("/assets/index-abc123.js", headers={"accept-encoding": "gzip;q=0"})

        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
        assert resp.content == original

    def test_wildcard_override_explicit_gzip(self, static_app):
        """`*;q=0, gzip;q=1` must still gzip because explicit gzip wins."""
        app, web_dist = static_app
        self._make_large_js(web_dist / "assets", "index-abc123.js")

        client = TestClient(app)
        resp = client.get(
            "/assets/index-abc123.js",
            headers={"accept-encoding": "*;q=0, gzip;q=1"},
        )

        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
