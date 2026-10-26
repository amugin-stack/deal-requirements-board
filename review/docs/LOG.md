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

## Commits

- 9334de4: graph doc + frozen `check_success.py` / `check_blind.py` (blind writer saw only the brief + EM findings).
- 02a9402: sensitivity + iterations scripts and output.
- 378c441: README join of writer A (results) and writer B (EM fixes, stale sections). Lead changes at the join:
  A's "baseline looks much better than on either holdout" narrowed to "than on holdout v2" (on v3 the
  baseline's timeline is 100%); iterations.py source line reworded (it read as a nonexistent `results/claude_` path).

## Adversarial review (read-only reviewer, 17 findings) and dispositions

Each finding was checked by the lead before any change. All generated blocks matched a fresh run.

| # | Sev | Finding | Lead verification | Disposition |
|---|---|---|---|---|
| 1 | med | "no test loads `.env`" is false | `tests/test_packaging_compat.py:131` runs `-m deal_board.cli.eval`, whose `run()` calls `load_dotenv_file()` | **Fixed** (claim removed). Test itself is out of scope: follow-up for the orchestrator |
| 2 | med | missing_fields p 0.00781 doesn't survive multiple-comparison correction | 8 fields; Bonferroni threshold is below 0.00781 | **Fixed**: README says no field separates after a Bonferroni correction (no hand-typed threshold) |
| 3 | med | v2 first-vs-final cell-level p overstates (cells within a post move together) | confirmed; h01/h02 errored, all cells move | **Fixed**: `iterations.py` now prints a post-level test (3 / 1, p = 0.625) and the header caveat; README quotes it |
| 4 | med | "within noise" cited non-overlapping min–max spans; iteration 3 also separated | confirmed from iterations.txt | **Fixed**: noise claim now rests on p only; iteration 3 (p 0.0117, reverted) stated |
| 5 | med | "strict mode fixed h01/h02", "lender rule fixed h12" overclaim | h02 still misses timeline; v2 has only first/final runs, so no iteration attribution | **Fixed**: reworded; three posts gained, one worse |
| 6 | med | Gate row omits the 90-day TREC snapshot rule | `deal_board/matcher.py:42,64` `MAX_SNAPSHOT_AGE_DAYS = 90`; all 200 rows are `trec_2026-10-20` | **Fixed**: Gate row + new Limitations item |
| 7 | med | s14's label itself is questionable (EM-8, deferred); s15 "near Boise" keeps Boise | `review/fixes/dispositions.json` EM-8 deferred; `data/posts.jsonl` s15 city Boise | **Fixed**: both disclosed; no label changed |
| 8 | low | "~4 points" vs one post; keep rule omits non-deal fills | dev has 19 deal posts; iteration 3 reverted on fills (loop/state.json) | **Fixed** (no hand-computed 5.3 figure) |
| 9 | low | "fixed temperature" misleading | loop/DRAFT.md: only the default temperature is accepted | **Fixed** |
| 10 | low | "Neither labeler saw…" overstates; it's self-reported, and holdout.jsonl's first-line keys were seen | `docs/holdout_v3_agreement.md:9` | **Fixed** |
| 11 | low | mutation summary omits non-convergence and the post-fix round; test list incomplete | `review/mutants/REPORT.md:12-21`; `review/fixes2/POSTREVIEW.md:26` | **Fixed**, cited to those files |
| 12 | low | "every results file records token usage" contradicts v2 note; v2 lacks drift hashes | tables.py stderr warnings | **Fixed** |
| 13 | low | API comment implies `.env` key use | `deal_board/config.py:31`: only CLI entry points load `.env` | **Fixed** |
| 14 | low | error analysis is one run; s12 property_type omitted | `results/claude_20261024T234359Z.json` failures | **Fixed** |
| 15 | low | v2 disclosure repeated, and sat under "Baseline caveat" | confirmed | **Fixed**: own `## Holdout v2` section; duplicate removed |
| 16 | low | "11 field failures" includes 5 missing_fields lines | `results/claude_20261026T003610Z.md` | **Fixed** |
| 17 | low | "rigorous" is self-praise | — | **Fixed** (word removed; quoted company tagline kept as the owner wrote it) |

Note on traceability: a few counts in Engineering notes (mutation 160 / 45 / 13 / 4 rounds / 39) trace to committed
`review/` reports rather than `results/`; they are cited inline. The 90-day rule traces to `deal_board/matcher.py`.
