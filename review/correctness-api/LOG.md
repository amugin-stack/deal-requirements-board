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
