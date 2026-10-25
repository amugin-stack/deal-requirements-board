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
  git history never held .env, data/raw/ or real names (only the `sk-ant-XXXX` test
  placeholder); /docs and /openapi.json expose only the schema; 404/405/413 bodies fixed;
  SDK logger pin holds after import; shared broker list never mutated; no cross-request state.
