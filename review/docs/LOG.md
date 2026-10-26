# Docs pass log (branch docs-v1)

## Setup

- Worktree `../deal-requirements-board-docs`, branch `docs-v1` from `main`, fresh venv installed from this checkout.
- First test run on `main` d30261c: `29 failed, 1466 passed, 1 skipped, 27 errors in 45.21s`. Cause: the
  final-eval tool tests used the real git-history scan, which now sees the committed holdout_v3 looks.
  Reported to the orchestrator; owner chose a separate fix branch (`fix-eval-tests`, c721f75, merged to
  `main` at 6fdaba8). During that fix's guard proof I briefly appended a byte to the committed
  `results/regex_20261026T003559Z.json` and restored it from a copy (`git diff --quiet HEAD -- results`
  confirmed byte-identical); disclosed to the orchestrator.
- `docs-v1` rebased onto 6fdaba8. Own run: `1522 passed, 1 skipped in 43.02s`.

## Generated sources

- `review/docs/sensitivity.py` -> `sensitivity.txt`: city accuracy with v3-09 and v3-16 excluded.
- `review/docs/iterations.py` -> `iterations.txt`: prompt-loop table for EM-3 (dev means match
  `loop/state.json`; pooled p-values match EM-3's reproduction output: 1->2 p 0.791, 2->4 p 0.0703).
- README example `/match` responses re-checked with `DEAL_EXTRACTOR=regex` via TestClient (offline, no key):
  identical, including `excluded_for_licensing` 225 / 226.
