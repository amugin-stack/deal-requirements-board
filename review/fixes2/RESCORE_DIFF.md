# Rescore before/after

Command, run on `main`'s code (before) and on this branch (after), writing only to the scratchpad:

    deal-eval --rescore results/claude_20261024T234359Z.json --results-dir <scratchpad>/rescore_{before,after}

Verdict: **identical.** Comparing the two saved JSONs key by key, only `timestamp` and the `git`
stamp (commit hash) differ; `datasets`, `scoring`, `raw_check`, `source_scoring` and every per-post
record match, and the two Markdown reports are identical below their header lines.

Expected, since this branch changes no product code: `git diff --stat main..HEAD -- deal_board` is
empty. All changes are new tests under `tests/` and the loop's own files under `review/fixes2/`.
`results/` was not written (rescores went to the scratchpad only).
