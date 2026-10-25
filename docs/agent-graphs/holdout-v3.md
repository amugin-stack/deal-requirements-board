# Agent graph: holdout v3

Holdout v3 is a fresh, double-labeled evaluation set. Its purpose is independence:
the dev set, holdout v2 and the extractor prompt were all written by Claude, so they
may share blind spots. This set is written and labeled without anyone seeing them.

## Isolation rule

Until labels are final, no agent in this graph reads `extractors/`, `data/posts.jsonl`,
`data/holdout.jsonl` (beyond the first line's keys, for the record shape), `results/`,
`loop/`, `README.md`, `HANDOFF.md` or any test file. No extractor is run on this set.
The only rule source is `schema.py`.

## Graph

```mermaid
flowchart TD
    L1["1. Lead (Opus)<br/>graph doc + test skeleton<br/>commit"]
    A["2. Subagent A (Sonnet)<br/>input: schema.py text + record shape + variety list<br/>output: 25 posts v3-01..v3-25 with labels + label_note"]
    B["3. Subagent B (Opus)<br/>input: schema.py text + the 25 texts only<br/>output: independent labels + label_note"]
    J["4. Join (Lead)<br/>per-field raw agreement + Cohen's kappa<br/>docs/holdout_v3_agreement.md<br/>data/holdout_v3.jsonl with adjudication key"]
    T["5. Lead<br/>finish tests/test_holdout_v3.py<br/>pytest -q, commit"]
    R["6. Adversarial reviewer (read-only)<br/>rule leaks, near-duplicates, mislabeled non-deals,<br/>states from cities, null amounts, timeline boundaries"]
    V["Lead verifies each finding<br/>changes logged in agreement doc, commit"]
    P["7. Proof step<br/>set a non-deal post's state to TX,<br/>show failing test, restore"]

    L1 --> A
    A -- "texts only (labels withheld)" --> B
    A -- "labels A" --> J
    B -- "labels B" --> J
    J --> T --> R --> V --> P
```

## Who sees what

| Agent | Sees | Never sees |
|---|---|---|
| A (Sonnet) | `schema.py` text, record shape, variety list | any existing data, extractor, results |
| B (Opus) | `schema.py` text, A's 25 texts | A's labels and notes, any existing data |
| Join (lead) | A and B outputs | extractors, results (until labels final) |
| Reviewer | final `data/holdout_v3.jsonl`, `schema.py`, agreement doc | extractors, other data files |

Disagreements are not resolved silently: each record carries `"adjudication":
"agreed" | "lead-resolved"`, and the agreement doc lists every disagreement with the
schema rule the lead used, so the owner can overrule.
