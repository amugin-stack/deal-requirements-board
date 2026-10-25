# Fix loop: agent graph

Branch `fix-loop`, cut from `main` at `c47ee1f`. Input: the 59 confirmed findings in
`review/{correctness-api,compliance-security,eval-quality}/findings.json`, plus the 5 red-team strict
xfails (`RT-*`) and the 5 P10 packaging leftovers (`P10-*`). Success is defined once, in
`review/fixes/check_success.py`, committed first and never edited.

Finding ids collide across the three reviews (`C-4` exists in both correctness-api and
compliance-security), so every disposition is keyed by `(review, id)`.

```mermaid
flowchart TD
    A[Commit 1: graph.md + frozen check_success.py] --> B[Triage commit: dispositions.json<br/>every finding planned / wontfix / deferred<br/>+ RT-* and P10-* extras]
    B --> S1 & S2 & S3 & S4

    subgraph Sets[Disjoint file sets, run in parallel]
      S1[Set 1: matcher + followup + brokers + schema + build_brokers]
      S2[Set 2: api + config]
      S3[Set 3: scoring + cli/eval + extractors/claude runtime + loop guard]
      S4[Set 4: packaging: pyproject, CI, .gitignore, paths, agent file, README table]
    end

    S1 --> I1[Implementer: one commit per finding] & T1[Blind test-writer:<br/>sees claim + proposed_regression_test only]
    S2 --> I2[Implementer] & T2[Blind test-writer]
    S3 --> I3[Implementer] & T3[Blind test-writer]
    S4 --> I4[Implementer] & T4[Blind test-writer]

    I1 & T1 & I2 & T2 & I3 & T3 & I4 & T4 --> J[Join: pytest / ruff / mypy<br/>fix implementation unless a test contradicts the claim<br/>wrong finding -> wontfix with evidence<br/>remove red-team xfail only when its test passes]

    J --> R[Adversarial reviewer, read-only, main..HEAD:<br/>licensing-gate regressions, rescore drift,<br/>env reads outside config.py, weakened tests, churn]
    R --> RS{rescore of results/claude_20261024T234359Z.json<br/>into scratchpad: before == after?}
    RS -- differs, no finding explains it --> J
    RS -- identical or delta recorded --> P

    P[Post-fix review round n:<br/>eval-method reviewer + compliance reviewer, read-only<br/>one verifier per candidate] --> N{new_medium_or_high == 0?}
    N -- no, round < 3 --> F[Fix with regression tests] --> P
    N -- no, round == 3 --> X[Stop and report to orchestrator]
    N -- yes --> Q[Record POSTREVIEW.md]
    Q --> Z[Proof: check_success.py output, rescore diff,<br/>xfail-revert demo; orchestrator verifies and merges]
```

## Rules every node follows

- Never touch `results/`, `data/*.jsonl|csv`, the frozen baseline, `review/` outside `review/fixes/`,
  or the Claude system prompt / tool schema / schema field descriptions (`PROMPT_VERSION` stays `ac1a145b`).
- No `.env`, no API keys, no network calls, nothing evaluated on `data/holdout_v3.jsonl`.
- Small single-purpose commits naming finding ids; typed code; docstrings that say why.
