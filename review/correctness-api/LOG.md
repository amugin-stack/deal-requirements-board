# Review loop log

One section per round. Each round: two read-only reviewers in parallel (correctness, api_contract),
then one blind verifier per candidate that reproduces the claim from the scratchpad
(`python -I`, importing from the worktree; TestClient for API claims; no network, no key).
After the verifiers, the lead re-ran every recorded reproduction (`verify/<id>/final.py`) and
stored its real output in `findings.json`.

## Round 1

Surfaces: open brief per dimension (correctness: the six modules; api_contract: `api.py` vs
`docs/agent-graphs/api.md` and `README.md`). Known xfails excluded.

| id | dimension | verdict | severity (verifier) | one line |
|----|-----------|---------|---------------------|----------|
| C-1 | correctness | confirmed | low (was medium) | max_tokens-truncated tool call accepted as a valid extraction |
| C-2 | correctness | rejected | - | datetime `as_of` crashes `is_licensed`: no caller can pass one |
| C-3 | correctness | rejected | - | lax coercion in `parse_requirement`: pinned as intended by red-team tests |
| C-4 | correctness | confirmed | low | `missing_fields` stale after `model_copy(update=)` / assignment |
| C-5 | correctness | confirmed | low | property-management posts always asked buy/sell/lease/1031 |
| C-6 | correctness | confirmed | low | market match misses 'Austin, TX', 'Ft. Lauderdale', NBSP |
| C-7 | correctness | confirmed | low | `load_brokers` aborts on a BOM or blank row, no row context |
| A-1 | api_contract | confirmed | low (was medium) | extraction-failure 422 body (`detail: str`) not in OpenAPI |
| A-2 | api_contract | confirmed | low (was medium) | post-extraction errors give text/plain 500, not ErrorDetail JSON |
| A-3 | api_contract | confirmed | low (was medium) | OpenAPI length bounds are raw; server checks after stripping |
| A-4 | api_contract | rejected | - | malformed Content-Length → 413: h11 rejects it with 400 first |
| A-5 | api_contract | rejected | - | cached extractor keeps the first key: documented trade-off |

candidates: 12
confirmed: 8
rejected: 4
new_confirmed: 8

## Round 2

Reviewers were given the round-1 ledger (confirmed and rejected, with rejection reasons) and
asked for different, systematically enumerated surfaces. Both returned an empty candidate list,
so no verifiers ran. Probe scripts are in the scratchpad (`r2-correctness/`, `r2-api/`).

Surfaces probed that held:

- **correctness:** expiration == as_of is inclusive, including leap days (2028-02-29); bad CSV date
  formats are rejected; status `"Active "`/`"active"` and lowercase `license_state` fail closed or are
  rejected; `excluded_for_licensing` is right for non-deal, no-state, empty and duplicate lists;
  `limit` 0, negative and larger than the list; ties break on broker_id; deal size at min/max and 0;
  min > max never matches; unicode whitespace in city/notes/state; enum names are rejected;
  every follow-up branch; `_strict_schema` keys; `PROMPT_VERSION` stability; first `tool_use` taken;
  non-dict tool input becomes ExtractionError; `_split_list` and invalid list enums.
- **api_contract:** 4000/4001 code points (ASCII, emoji, combining marks); unicode-whitespace
  stripping; lone surrogates give a clean 422; 65536/65537-byte bodies; 4000 astral chars as
  escapes (48011 bytes) still 200; chunked bodies; README example response reproduced exactly
  (225 excluded of 226); published ValidationError schema has `input` optional; no sentinel post
  text in any log record at DEBUG on every error path; every `anthropic.APIError` subclass → 503
  with a fixed body; cached broker list unchanged; 8 threads × 20 posts byte-identical.

candidates: 0
confirmed: 0
rejected: 0
new_confirmed: 0

## Result

Converged after 2 rounds: 12 candidates, 8 confirmed (5 correctness, 3 api_contract), 4 rejected
(2 correctness, 2 api_contract). Every confirmed finding is rated low by its verifier.
