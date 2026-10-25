# Compliance & security review loop

Read-only adversarial review of the deal board over two dimensions: **compliance** (can an
unverifiable broker ever be returned or ranked?) and **security & data** (secrets, logs, leaks,
PII, injection, DoS). No existing file is changed; every claim is reproduced before it counts.
Success is defined once, in `check_success.py`, and that file is frozen after the first commit.

```mermaid
flowchart TD
    A[Commit 1: graph.md + check_success.py<br/>success definition frozen] --> R

    subgraph R[Round N]
        direction TB
        P[Prior confirmed + rejected lists<br/>and surfaces already covered] --> RC & RS
        RC[Reviewer: compliance<br/>read-only subagent] --> C[Candidate findings<br/>status: candidate]
        RS[Reviewer: security & data<br/>read-only subagent] --> C
        C --> V1[Verifier per candidate<br/>python -I script in scratchpad<br/>TestClient / synthetic Broker / CSV scan<br/>no network, no key]
        V1 --> D{Reproduced?}
        D -- yes --> CF[confirmed<br/>+ proposed_fix<br/>+ proposed_regression_test]
        D -- no --> RJ[rejected<br/>+ reason]
    end

    CF & RJ --> L[Append round to LOG.md<br/>candidates / confirmed / rejected / new_confirmed<br/>update findings.json + findings.md]
    L --> K[Commit the round]
    K --> Q{new_confirmed == 0<br/>or round == 4?}
    Q -- no: try new surfaces --> R
    Q -- "yes, converged" --> F[findings.md: severity table,<br/>fix-first list, compliance promise audit]
    Q -- "round 4, still finding" --> NC[Report non-convergence]
    F --> S[Proof: run check_success.py → all PASS<br/>corrupt one snippet → FAIL → restore]
    NC --> S
```

Rules the loop never breaks: no edits outside `review/compliance-security/`, nothing in
`results/` touched, `.env` never opened, no API calls, no scraping, no pushes or merges.
