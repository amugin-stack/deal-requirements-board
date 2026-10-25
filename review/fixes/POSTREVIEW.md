# Post-fix review

After all fix sets were joined and the adversarial review's three findings (ADV-1..3) were fixed, the
fixed code was reviewed again by two read-only reviewers (eval methodology, compliance/security) for
NEW or REMAINING medium/high issues. Every candidate went to its own independent verifier, who
reproduced it and set the final severity. Confirmed findings are fixed with regression tests (blind
test-writer, then join) and recorded in `dispositions.json` under review `postreview-<n>`.

## Round 1

new_medium_or_high: 6

Reviewed at `7f4f951`. Reviewers: eval-method (6 candidates) and compliance (1 candidate).

| id | reviewer | claimed | verified | finding | fix |
|---|---|---|---|---|---|
| EVAL-1 | eval | medium | medium | EM-6 excluded relabeled posts from rescores, so a run made against a doctored label reproduced its stored score and passed CQ-7's rescore check | `e7b7deb`, `5e08d30` |
| EVAL-2 | eval | medium | medium | the rescore check ignored coverage: a run with its misses deleted reproduced its own rates | `5e08d30` |
| EVAL-3 | eval | medium/high | medium | the final gate had neither the freshness nor the rescore check | `5e08d30` |
| EVAL-4 | eval | medium | medium | a run made at a commit later `reset` away still counted while the history check no longer saw the commit | `5e08d30` |
| EVAL-5 | eval | medium | medium | a dirty final look was never counted, inviting a second clean look | `5e08d30` |
| EVAL-6 | eval | medium | medium (rare) | the strict output check followed the prompt version, so a strict run's errors vanished after the next prompt change | `e7b7deb` |
| COMP-1 | compliance | medium (borderline) | low | a lone UTF-16 surrogate from the extractor validated, then broke response serialization outside the route's error handling | `52e0e55` |

Regression tests: `tests/test_fix_postreview1.py` (25, written blind from the contracts, 19 red on the
pre-fix commit, all green on the fix) and `tests/test_fix_adversarial.py::test_comp1_*`. After the
fixes: 1318 passed, 1 skipped; ruff, format, mypy clean; all 21 saved runs still rescore identically to
`main` apart from the intended EM-61 keys.

Rejected or low-only observations from round 1 (not fixed, recorded for the owner):
- Follow-up echoes the poster's own spelling of a known city (e.g. full-width letters); only known-city
  keys can be echoed, so no injected words.
- `.gitignore` doesn't cover `.envrc` / `*.env`; eval CLI doesn't pin the SDK loggers like `api.py`
  does (`ANTHROPIC_LOG=debug` would print post text in eval only); `</post>` in a post isn't escaped
  in the prompt wrapper (prompt text is frozen by PROMPT_VERSION).
- Loop guard: `git update-index --skip-worktree` hides edits from git-based checks; `tests_check`
  doesn't bound xfails; `split` isn't part of the post hashes; the shipped loop can no longer pass its
  own (stricter) guard, which is expected: legacy runs carry no `request_sha` or cache flags.
- README Gate row doesn't mention the 90-day snapshot rule or the New York date (docs builder).
