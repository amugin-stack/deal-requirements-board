"""The broker-facing page at `/`: served safely, self-contained, and additive to the API.

These tests guard the properties the page relies on, not its look:
- `/` and `/static/*` are served with the right types and a strict same-origin CSP;
- the page loads no third-party assets and never writes API values as HTML;
- the API surface (`POST /match`, its OpenAPI schema) is unchanged by the page.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deal_board import api, ui

STATIC = ui.STATIC_DIR
ASSETS = ("index.html", "app.css", "app.js", "mark.svg")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(api.app) as c:
        yield c


def test_static_files_ship_with_the_package() -> None:
    for name in ASSETS:
        assert (STATIC / name).is_file(), name


def test_index_is_html_with_a_strict_csp(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "<title>Deal Requirements Board" in r.text


@pytest.mark.parametrize(
    ("name", "ctype"),
    [("app.css", "text/css"), ("app.js", "text/javascript"), ("mark.svg", "image/svg+xml")],
)
def test_assets_are_served(client: TestClient, name: str, ctype: str) -> None:
    r = client.get(f"/static/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)


def test_static_mount_does_not_escape_its_directory(client: TestClient) -> None:
    for path in ("/static/../api.py", "/static/%2e%2e/api.py", "/static/..%2fapi.py"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "def match_post" not in r.text


def test_page_routes_are_not_in_the_api_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert list(paths) == ["/match"]


def test_page_loads_no_third_party_assets() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs, "expected asset references"
    for ref in refs:
        assert ref.startswith(("/", "#")), f"non-same-origin reference in index.html: {ref}"
    assert "<script>" not in html, "inline scripts would be blocked by the CSP"
    assert " style=" not in html, "inline styles would be blocked by the CSP"
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(http" not in css


def test_script_never_writes_html() -> None:
    """API values can echo post text, so the page must only ever use textContent and DOM nodes."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js, sink


def test_script_stores_nothing_in_the_browser() -> None:
    """Posts can hold client details; the page keeps them only in the open tab."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for store in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert store not in js, store


def test_script_calls_only_match() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert re.findall(r'fetch\("([^"]+)"', js) == ["/match"]


def test_script_limits_agree_with_the_api() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert f"const MAX_CHARS = {api.MAX_POST_CHARS};" in js
    assert f'maxlength="{api.MAX_POST_CHARS}"' in html


def test_script_follow_up_priority_matches_the_server() -> None:
    """The page labels the asked field using the same order `followup.py` asks in."""
    from deal_board.followup import PRIORITY

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const PRIORITY = \[([^\]]+)\];", js)
    assert m is not None
    assert tuple(re.findall(r'"([a-z_]+)"', m.group(1))) == PRIORITY


def test_script_fit_maximum_matches_the_matcher_points() -> None:
    """The page shows 'N of 8'; 8 is property +3, transaction +2, market +2, deal size +1."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const MAX_FIT = 8;" in js
    readme = (Path(api.__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "property type +3, transaction type +2" in readme


def test_match_still_works_alongside_the_page(client: TestClient) -> None:
    api.app.dependency_overrides[api.get_extractor] = api._regex_extractor
    try:
        r = client.post(
            "/match", json={"text": "Need a buyer's agent in Phoenix, AZ for an SFR up to $450k, closing in 30 days."}
        )
    finally:
        api.app.dependency_overrides.pop(api.get_extractor, None)
    assert r.status_code == 200
    body = r.json()
    assert body["requirement"]["state"] == "AZ"
    assert set(body) == {"extractor", "requirement", "matches", "excluded_for_licensing", "follow_up_question"}
