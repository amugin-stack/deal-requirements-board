"""The broker-facing web page: a single static screen that calls `POST /match`.

The page is plain HTML, CSS and JavaScript in `deal_board/ui_static/`, with no build step and no
third-party assets. It talks only to this app's own `POST /match`, so the API contract and
business rules stay exactly as they are; this module just serves files.

The page is served with a strict Content-Security-Policy: scripts, styles and requests are
same-origin only and no inline script runs. The page renders API values with `textContent`,
never as HTML, because the requirement can echo text from the post (for example a city name).
Nothing the broker types is stored in the browser or logged by the page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Final = Path(__file__).parent / "ui_static"

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

PAGE_HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def install(app: FastAPI) -> None:
    """Serve the page at `/` and its assets under `/static/`. Neither appears in the OpenAPI schema."""

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=PAGE_HEADERS)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
