# Agent graph: final-eval protocol tooling (`tools/final_eval/`)

Goal: make the one-shot `holdout_v3` eval mechanical, auditable and blind to its outcome.
Branch `final-eval-tools`, worktree `../deal-requirements-board-final-eval`, cut from `main` at `c47ee1f`.
Success is defined by `check_success.py`, committed here in commit 1 and never edited after.

```mermaid
flowchart TD
    L0["Lead: graph.md + frozen check_success.py<br/>(commit 1)"] --> L1["Lead: CLI contract in PROTOCOL.md<br/>flags, exit codes, hooks, results shape (commit 2)"]
    L1 --> I1["Implementer A<br/>run.py + validate.py"]
    L1 --> I2["Implementer B<br/>tables.py"]
    L1 --> T["Blind test writer<br/>tests/test_final_eval_tools.py<br/>sees contract + existing results files only"]
    I1 --> J["Join (lead)<br/>pytest, ruff, ruff format, mypy --strict green"]
    I2 --> J
    T --> J
    J --> R["Read-only adversarial reviewer<br/>one-look bypasses, key leaks, dirty runs accepted,<br/>v2/v3 mixing, timestamp collisions"]
    R --> F["Lead: verify each finding, then fix + regression test"]
    F --> P["Proof: check_success.py output + tables.py on real v2 files"]
```

## Rules every node follows

- Only these paths change: `tools/final_eval/{__init__,run,validate,tables,check_success}.py`,
  `tools/final_eval/{graph,PROTOCOL}.md`, `tests/test_final_eval_tools.py`. No existing file is edited.
- No extractor is ever run on `data/holdout_v3.jsonl`, not even the regex baseline. Tests use a fake
  extractor on synthetic posts in temp dirs. No API calls. `.env` is never opened.
- Nothing under `results/` or `.cache/` is written, edited or deleted.
- The blind test writer never reads `run.py`, `validate.py` or `tables.py`; it tests the contract.
- The reviewer is read-only; each finding is reproduced before it is fixed, and each fix gets a
  regression test.
- The lead never merges or pushes; the orchestrator does, with the owner's approval, and is the one
  who runs the real final eval.
