# Agent graph: mutation-testing loop on the licensing gate and match path

Does the test suite actually protect the compliance promise ("never return a broker who can't be
verified as licensed in the deal's state")? We find out by breaking the code on purpose. Each
mutant is a small unified diff to `deal_board/matcher.py`, `deal_board/api.py` or
`deal_board/brokers.py`; a mutant is **killed** when at least one test fails on it and
**survives** when the whole suite stays green.

Mutants are never applied to the worktree. The runner copies the worktree into the scratchpad,
gives each copy its own venv with `pip install -e`, patches the copy, runs the full suite there,
and restores the copy. No existing file in the repo changes; everything lives in `review/mutants/`.
Branch `gate-mutants`, cut from `main` at `c47ee1f`.

```mermaid
flowchart TD
    C1["Lead: graph.md + check_success.py<br/>(commit 1; the script is frozen from here)"]
    C1 --> RUN0["Lead: mutants.py runner<br/>baseline on an unpatched copy must be<br/>1003 passed / 1 skipped / 5 xfailed"]
    RUN0 --> R

    subgraph ROUND [Round n]
        R{{"Lead: brief mutant writers<br/>(round > 1: kill list + survivors,<br/>ask for untouched / surviving regions)"}}
        subgraph WRITE [Parallel mutant writers, read-only, one message]
            WG["Gate writer: is_licensed,<br/>date / status / state comparisons"]
            WM["Match/API writer: match(), _score,<br/>excluded count, api.py reshaping"]
        end
        R --> WG & WM
        WG & WM --> CAND["Candidate mutants<br/>(unified diffs vs worktree files)"]
        CAND --> EXEC["Lead: runner applies each diff to a<br/>scratchpad copy, full suite, records<br/>failing node ids (killed_by)"]
        EXEC --> SURV{"survived?"}
        SURV -- "no" --> K["status: killed"]
        SURV -- "yes" --> VER
        subgraph VER [One verifier per survivor, parallel]
            V1["Verifier k: ~5-line script, original vs<br/>mutant on one input; differs -> survived<br/>(+ severity, proposed test, fictional fx-* fixture);<br/>same -> equivalent (excluded from the 25)"]
        end
        K & VER --> J["Lead: results.json + LOG.md round<br/>(new_high_medium_survivors: k) (commit)"]
    end

    J --> D{"new_high_medium_survivors == 0<br/>or round == 4?"}
    D -- "no" --> R
    D -- "yes" --> M["Lead: REPORT.md (table, survivors,<br/>proposed tests/fixtures, verdict) (commit)"]
    M --> P["Proof: runner on always-True gate (~81 red-team + more fail)<br/>and on one survivor (0 fail);<br/>check_success.py PASS on the final commit"]
```

## Why this shape

- **Success is defined before any mutant runs.** `check_success.py` is committed first and never
  edited; its check (g) fails if it changes.
- **Mutants are real.** Check (c) re-applies every stored diff to a temp copy of today's file and
  requires the file to change, so a hand-waved or stale mutant fails the check.
- **A survivor needs evidence.** A mutant that passes the suite might just be equivalent to the
  original. A separate verifier must show an input where the two behave differently before the
  survivor counts; otherwise it is marked `equivalent` and doesn't count towards the 25.
- **Severity follows the compliance promise.** `high` = the mutant can return an unlicensed,
  expired or out-of-state broker; `medium` = wrong counts, notes or ranking a client would see;
  `low` = cosmetic or unreachable in practice.
- **Proposals, not edits.** Survivors get a proposed test and, when needed, an obviously
  fictional `fx-*` fixture row. Nothing under `deal_board/`, `tests/` or `data/` is changed,
  because a fix-loop session owns those files.
