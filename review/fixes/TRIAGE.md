# Triage

`dispositions.json` holds one entry per confirmed finding (keyed by `review` + `id`, since ids collide
across reviews), plus extras `RT-1..5` (the red-team strict xfails) and `P10-1..5` (packaging leftovers).
At triage every entry is `planned`, `wontfix` or `deferred`; `note` is the one-line plan (the contract the
implementer and the blind test-writer both work from).

Counts at triage: 59 review findings -> 46 planned, 1 wontfix, 12 deferred; extras 10 planned.

## Disjoint file sets

| set | files | findings |
|---|---|---|
| A gate | `deal_board/{matcher,followup,brokers,schema}.py`, new `deal_board/cities.py`, `deal_board/extractors/__init__.py` (docstring), `deal_board/cli/build_brokers.py`, `tests/test_data.py` | corr C-4, C-6, C-7; comp C-2, C-4, C-5, C-6, C-8 (pin only), C-9; eq CQ-22, CQ-51; RT-1, RT-3, RT-4 |
| B api | `deal_board/{api,config}.py`, `tests/test_api.py` | corr A-1, A-2, A-3; comp C-3, C-7, C-13, S-1, S-2, S-3; RT-2, RT-5; P10-3 |
| C1 eval | `deal_board/scoring.py`, `deal_board/cli/eval.py`, `deal_board/extractors/claude.py` (runtime code only, never `SYSTEM_PROMPT`/`TOOL`), `tests/test_{eval,eval_compare,eval_cache,scoring_stats}.py` | corr C-1; comp C-10, S-4; eq CQ-2, CQ-3 (eval side), CQ-21, CQ-23, CQ-24, CQ-41, CQ-42, CQ-43, CQ-5, CQ-52, CQ-61, CQ-8, EM-1 (eval side), EM-23, EM-24, EM-6, EM-61 |
| C2 loop guard | `loop/check_success.py`, `tests/test_check_success.py` | eq CQ-3 (gate side), CQ-31, CQ-4, CQ-7, EM-1 (gate side), EM-21, EM-5 |
| D packaging | `pyproject.toml`, `.gitignore`, `.github/workflows/ci.yml`, `deal_board/paths.py`, `deal_board/extractors/regex_baseline.py`, README architecture table, `.claude/agents/extractor-improver.md` (paths) | comp S-6, S-7; P10-1, P10-2, P10-4, P10-5 |

Set C is split into two implementers on disjoint files (C1, C2); C2 codes against C1's contract
(`request_sha`, `call.cached`, `rescored_from`, `cli.eval.rescore`).

`tests/test_red_team.py` is owned by the join: it removes the five xfail marks once RT-1..5 pass, and moves
the `state-int` / `state-bytes` cases to the fixed 422 because comp C-6 makes non-string states raise.

Blind test-writers get only each finding's `claim`, its `proposed_regression_test` and the plan line
above; they write new `tests/test_fix_*.py` files (plus the test-only findings' own files) at the
triage commit, never seeing an implementation diff.
