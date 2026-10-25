# Review loop: eval method + code quality

Read-only adversarial review of branch `review-eval-quality`. Nothing outside `review/eval-quality/`
changes; `check_success.py` (committed first, then frozen) is the single definition of done.

```mermaid
flowchart TD
    S[Commit 1: graph.md + check_success.py<br/>frozen from here on] --> R

    subgraph R[Round N]
        direction TB
        P1[Reviewer A: eval_method<br/>read-only subagent] --> C[Candidate findings<br/>status: candidate]
        P2[Reviewer B: code_quality<br/>read-only subagent] --> C
        C --> V[One adversarial verifier per candidate<br/>python -I script in scratchpad<br/>reads results/*.json read-only<br/>no network, no key, no holdout_v3 eval]
        V -->|reproduced| OK[confirmed<br/>+ proposed_fix + regression test]
        V -->|not reproduced / not a defect| NO[rejected + reason]
        OK --> L[Append round to LOG.md<br/>candidates / confirmed / rejected / new_confirmed]
        NO --> L
        L --> CM[Commit findings.json, findings.md, LOG.md]
    end

    CM --> D{new_confirmed == 0<br/>or round 4?}
    D -->|no: pass confirmed + rejected lists,<br/>reviewers must try new surfaces| R
    D -->|yes| F[findings.md: severity table,<br/>README numbers audit, what to fix first]
    F --> CK[python review/eval-quality/check_success.py<br/>a-g all PASS]
    CK --> PR[Proof: corrupt one snippet -> FAIL, restore -> PASS]
    PR --> H[Report to orchestrator<br/>no push, no merge]
```

Round 4 without a zero round is reported as non-convergence, not papered over.
