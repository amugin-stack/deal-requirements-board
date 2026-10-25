# Review loop log

Paths in commands: `$WT` = this worktree, `$PY` = the main checkout's `.venv/bin/python`
(Python 3.14), `$SCRATCH` = the session scratchpad holding every verifier script (`roundN/verify_<ID>.py`).
Each round: one compliance reviewer and one security & data reviewer in parallel (read-only), then
one adversarial verifier per candidate that reproduces it with its own `python -I` script before
marking it confirmed or rejected. No network, no key, no repo edits.

## Round 1

- candidates: 11 (compliance 6: C-1..C-6; security_data 5: S-1..S-5)
- confirmed: 10 — C-2, C-3, C-4, C-5, C-6, S-1, S-2, S-3, S-4, S-5 (all low after verification;
  C-2 was downgraded from medium)
- rejected: 1 — C-1 (Washington, DC → WA): duplicate of known issue #3; "DC" is itself a state
  token, and reordering to "DC (Washington)" routes to DC, so the cause is first-state-wins.
- new_confirmed: 10
- Surfaces checked and clean: is_licensed boundaries (expiry day, leap day 2028-02-29, None
  expiration, non-midnight datetimes rejected at load); Broker loader rejects lowercase/padded
  license_state; no duplicate broker_id/license_number; county map TX-only and post-gate; Claude
  prompt/tool never infer state, strict enum; .env never loaded by api.py; fake key absent from
  reprs, exceptions, 503 bodies, DEBUG logs; 422 handler leaks nothing via ctx/loc/msg; body cap
  handles +5/-1/Unicode digits/chunked/understated length; max legit post (4000 astral chars,
  escaped) = 48,012 B < 64 KiB; combining/zero-width/RTL inputs ~2 ms, worst regex ~0.29 s;
  results/*.json hold no paths/usernames/keys; RawCache names are sha256, files 0600, .cache/
  ignored; posts data has no phones/emails/URLs; no test reads the real clock (suite passes with
  get_as_of pinned to 2026-10-24, 2028-06-01, 2099-01-01).

## Round 2

- candidates: 9 (compliance 5: C-7..C-11; security_data 4: S-6..S-9)
- confirmed: 7 — C-7 (downgraded medium→low), C-8, C-9 (downgraded medium→low), C-10, S-6, S-7, S-8
- rejected: 2 — C-11 (BrokerMatch "contact and verify" docstring: license_number + source is the
  public lookup key, so "verify" is honest; one-word nit below the bar); S-9 (username paths in
  *sibling* review branches: out of scope, `main` has none and check (g) matches its spec;
  passed to the orchestrator as a pre-merge note)
- new_confirmed: 7
- Surfaces checked and clean: four weakened gates (month-only expiry, 1-day grace,
  status endswith "Active", unknown expiry allowed) each fail the suite; model-supplied
  missing_fields is recomputed; first tool_use wins; follow-ups never imply a licensed match;
  git history never held .env, data/raw/ or real names (only the redacted key-prefix test
  placeholder); /docs and /openapi.json expose only the schema; 404/405/413 bodies fixed;
  SDK logger pin holds after import; shared broker list never mutated; no cross-request state.

## Round 3

- candidates: 3 (compliance 2: C-12, C-13; security_data 1: S-10)
- confirmed: 3 — C-12, C-13, S-10 (S-10 narrowed: DRAFT.md already names the Claude pre-loop
  holdout file; holdout_v3 was added after the loop finished, so no past leak)
- rejected: 0
- new_confirmed: 3
- Surfaces checked and clean: every non-null gold state in posts/holdout/holdout_v3 is named in
  the post text; scoring.py never rewards a guessed state; Claude prompt has no city→state
  examples; non-deal labels all-null; follow-ups none for non-deals; no broker has min > max;
  a bad CSV row fails the whole load (closed); no compliance test is vacuous except C-13's;
  lone surrogate → 422 before `_post_id`; NUL byte fine; Unicode spaces stripped consistently;
  Cyrillic/fullwidth state lookalikes fail closed; responses not amplified; `extractor`/`source`
  expose nothing internal; tests write nothing outside tmp_path (suite run in a scratch copy).

## Round 4

- candidates: 1 (compliance 0; security_data 1: S-11)
- confirmed: 0
- rejected: 1 — S-11 (eval `markdown()` embeds model output unescaped, so a city with a backtick
  and blank lines can inject markdown into results/*.md): mechanics reproduced, but the only
  inputs are owner-curated datasets, so no untrusted text reaches the sink; committed reports
  render fine.
- new_confirmed: 0
- Surfaces checked and clean: whole-suite mutation runs in a scratch copy — `.upper()` state
  compare, `.strip()`/`.lower()`/`startswith`/`in` status checks, deny-list statuses, 30-day
  grace, day=1 and (year, month) expiry, None expiry allowed, `state is None` allowed,
  non-deal early return removed, wrong excluded count, sales note removed: every one turns the
  suite red. Two survivors (`state in license_state`, `"Sales" in license_type`) are equivalent on
  validated data. Sales note only on gated brokers; README `excluded_for_licensing` examples
  match the live API; RawCache keys cover full post + prompt + extractor source; slow-body
  clients hold ≤ 64 KiB and no worker thread.

**Converged after 4 rounds** (last round confirmed nothing new).
