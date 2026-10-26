# Fix pass 2: closing the gate-mutant survivors

Branch `fix-pass-2`, cut from `main` at `219f39f`. Input: the 27 high/medium survivors
(`status == "survived"`, `severity` high or medium) in `review/mutants/results.json`: 13 high,
14 medium. Success is defined once, in `review/fixes2/check_success.py`, committed first and
never edited.

A survivor is a deliberate bug the suite did not notice. Closing one means a test that passes
on the shipped code and fails on the mutant. The proof is mechanical: `review/fixes2/rerun.py`
applies each mutant's diff to a scratchpad copy of the current tree, runs the full suite there,
and saves which tests failed in `rerun.json`. Most stored diffs were written against an older
`main` and no longer apply; for those, `dispositions.json` carries a rewritten diff that makes
the same bug in the current code, and the runner uses it.

```mermaid
flowchart TD
    A[Commit 1: graph.md + frozen check_success.py] --> B[Triage commit: dispositions.json<br/>27 survivors, each with a plan and,<br/>where the stored diff no longer applies,<br/>a rewritten equivalent diff]
    B --> S1 & S2 & S3

    subgraph Sets[Disjoint file sets, run in parallel]
      S1[Set A: api.py<br/>tests/test_api_mutants.py]
      S2[Set B: brokers.py + cli/build_brokers.py<br/>tests/test_brokers_mutants.py, tests/test_build_brokers.py]
      S3[Set C: extractors + matcher.py<br/>tests/test_matcher_mutants.py, tests/test_extractor_mutants.py]
    end

    S1 --> I1[Implementer: product fix only for a<br/>real fail-open path in shipped code] & T1[Blind test writer:<br/>sees description + proposed_test only,<br/>never the diff]
    S2 --> I2[Implementer] & T2[Blind test writer]
    S3 --> I3[Implementer] & T3[Blind test writer]

    I1 & T1 & I2 & T2 & I3 & T3 --> J[Join: pytest / ruff / mypy green]
    J --> M[rerun.py: every survivor diff on a scratchpad copy<br/>-> rerun.json, failing tests per id]
    M --> K{every high killed,<br/>every medium killed or deferred?}
    K -- no --> F[Strengthen the test, never the mutant] --> J
    K -- yes --> R[Adversarial reviewer, read-only, main..HEAD:<br/>tautological tests, scoring / rescore drift,<br/>env reads outside config.py,<br/>excluded_for_licensing arithmetic]
    R --> RS{deal-eval --rescore into scratchpad:<br/>scores before == after?}
    RS -- differs --> F
    RS -- identical --> P[Post-fix mutation round:<br/>fresh reviewer writes >= 15 new mutants<br/>against the fixed code, same regions,<br/>each verified to change behaviour]
    P --> N{new high/medium survivors == 0?}
    N -- no --> F2[Add tests] --> P
    N -- yes --> Q[POSTREVIEW.md + postreview_rerun.json]
    Q --> Z[Proof: check_success.py output + rerun summary;<br/>orchestrator verifies and merges]
```

Owner rules that bound every node: no edits to `results/`, `data/brokers.csv`, `data/*.jsonl`,
`review/mutants/`, `review/fixes/`, `tools/`, `README.md`, `loop/`; the frozen baseline stays
byte-identical to `baseline-v1`; `PROMPT_VERSION` stays `ac1a145b`; no API calls; no `.env`.
After each phase the session checks its context use and writes `HANDOFF.md` above 40%.
