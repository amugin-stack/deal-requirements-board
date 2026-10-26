# Broker page: design system and handoff

The broker-facing page at `/` (branch `ui-overhaul`). Before it, the only rendered interface was
FastAPI's generated Swagger page at `/docs`, and `/` returned `{"detail":"Not Found"}`. Designed and
built by Claude (Opus 5.5) at the owner's request; this note is AI-written.

## Audience, task, journeys

- **Who:** a real estate broker with a referral in another state.
- **Primary task:** paste a post, see who can legally take it and why, then act.
- **Journeys covered:** a complete post leads to ranked licensed brokers. A missing state leads to one question; answer it on the page and it is added to the post and re-run. A non-deal post is explained, nothing is matched. A state with nobody licensed in our data gets an honest empty state. Every documented API failure, offline, timeout and clipboard denial has a message and a recovery step.

## Direction: "registry ledger"

Calm and official, like a license record, not a SaaS dashboard, because the product's promise is
compliance. Principles taken (no assets copied) from GOV.UK error summary and summary list, USWDS
summary box, and the TREC license holder search.

| Token | Value | Use |
|---|---|---|
| Serif | Iowan Old Style / Charter / Georgia (system) | page and section titles, scores |
| Sans | system-ui | body and controls |
| Mono | ui-monospace | license numbers, counts |
| Scale | 13 / 15 / 17 / 21 / 28 / 36 px | `--text-xs` … `--text-2xl` |
| Space | 4 px grid: 4, 8, 12, 16, 24, 32, 48, 64 | `--s1` … `--s8` |
| Radius | 4 px | everything; pills only for status badges |
| Paper / ink | `#f6f3ec` / `#1b1e22` (dark: `#15181c` / `#ece7dc`) | page |
| Licensed | `#1d6646` on `#e5f0e8` | a broker you may refer to, fit points |
| Attention | `#855000` on `#fbefd6` | a question to answer, compliance notes, stale results |
| Danger | `#a3282a` on `#f9e4e1` | errors only |
| Link / focus | `#1b4d86` + `#ffcf5c` halo | links, focus rings |

Colour carries meaning only; structure comes from rules and type. No shadows except the copy toast.
No web fonts and no third-party requests: the page works offline from the app and needs no licences.

## Components

- **Error summary** at the top of the form (focused, `role="alert"`): what happened, what to do, a link to the field or a Try again button. The field gets an inline message and `aria-invalid`.
- **What we read** (`<dl>`): one row per extracted field. "Not in the post" for empty fields, and the asked field is marked "asked below". Provenance line says whether Claude (an AI model) or the regex baseline read it.
- **Broker row:** rank, name, Licensed badge, license number (mono, Copy), type, record source (fictional records say so; TREC records give the snapshot date and a TREC link), one line per fit point, compliance notes set apart, score "N of 8" with an 8-segment bar.
- **Follow-up callout:** the server's question, an answer field, "Add to post and run again". The answer is appended visibly to the post.
- **Loading:** `aria-busy`, button label and spinner, placeholder rows. Reduced motion stops the animation.

## Engineering notes

- `deal_board/ui.py` serves `deal_board/ui_static/` at `/` and `/static/`, outside the OpenAPI schema. `api.py` gains two lines at the end. `POST /match`, its schema and every business rule are unchanged.
- CSP `default-src 'none'`, same-origin scripts, styles and requests; no inline script or style. API values are written with `textContent` only. Nothing is stored in the browser.
- The page mirrors three server facts. `tests/test_ui.py` pins all three to the server: the 4,000-character limit, the follow-up priority order, and the 8-point maximum.
- `tests/test_ui.py` also pins headers, assets, no third-party references, no HTML sinks, no browser storage, path traversal on `/static`, and the unchanged API schema.

## Verification (2026-10-26, built-in browser, regex extractor)

- Desktop 1280, tablet 768, phone 375; light and dark; no horizontal scroll at 375 with extreme labels.
- Real server: complete post, missing state with the answer loop, Texas dense results, Montana with no licensed brokers, non-deal, empty post.
- Simulated in the page by stubbing `fetch` (checks the page's handling, not the server): 422 extraction, 413, 503 (both messages), 500, 502 HTML, network failure, offline and back online, a 45 s timeout, long labels, HTML injection in every field (no element created, no script ran), and clipboard granted and denied.
- Keyboard: skip link, tab order, ⌘/Ctrl+Enter submits, focus moves to the results heading or error summary.
- Contrast computed for all token pairs: text ≥ 4.8:1, field borders ≥ 4.3:1 (both themes).
- Same browser, same machine. New page: 48 KB decoded, 4 requests, one origin, DOMContentLoaded 46 ms. Swagger `/docs`: about 1.8 MB decoded, 4 requests including the jsDelivr CDN, 254 ms (its CDN files were cached). Swagger stays available at `/docs`.

## Limitations and next steps

- Not tested with a screen reader, on real phones, or in Safari or Firefox. Only Chromium in the built-in browser was used.
- No automated end-to-end browser tests: the repo has no JS toolchain (no Node), so behaviour was verified by hand in the browser and the static guards live in pytest.
- The Claude extractor path wasn't exercised in the browser, to avoid API spend. It uses the same response shape, and the provenance line handles both.
- No permission or sign-in states exist in the product (there is no auth). Clipboard permission is the only one.
- The page can't show the licensing check date: the API doesn't return it. Adding it would change the API contract, so I left it out.
- `screens/after/desktop-empty-post-error.jpg` and `desktop-loading.jpg` were captured before the last contrast fix (darker field border, `--ink-3` #6b7078 → #666b73).
