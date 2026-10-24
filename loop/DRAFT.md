# Extractor Improvement Loop: Agent Draft

The working spec the `extractor-improver` agent reads at the start of every iteration. Sections marked **FIXED** may only be changed by the human owner. Sections marked **EDITABLE** are the agent's to improve; that is how the loop gets better at improving.

## Goal (FIXED)

Make the Claude extractor (`extractors/claude.py`, model `claude-haiku-5-5`) reliably better than the frozen regex baseline at turning broker posts into `DealRequirement` records, without guessing fields the post doesn't support. This is Phase 5 of the roadmap.

## Success (FIXED)

Success is whatever `loop/check_success.py` says. Nothing else counts. Its thresholds:

| Stage | Data | Check | Bar |
|---|---|---|---|
| guard | repo | protected files unchanged since tag `loop-start`; baseline unchanged since `baseline-v1`; `pytest` green | all true |
| dev | `posts.jsonl`, split `dev` (24 posts), last 3 runs of one prompt | schema errors | 0 in every run |
| dev | same | `is_deal_request` accuracy | 100% in every run |
| dev | same | fields filled on non-deal posts | 0 in every run |
| dev | same | mean of `is_deal_request` + 6 key fields | ≥ 0.932 (the baseline's dev mean) |
| dev | same | weakest single field | ≥ 0.85 |
| final | `holdout.jsonl` (headline) + `posts.jsonl` all | exactly one run, of the prompt that passed dev | — |
| final | same | schema errors | 0 |
| final | `holdout.jsonl` | mean of `is_deal_request` + 6 key fields | ≥ 0.85 (baseline 0.764, start 0.805) |
| final | `holdout.jsonl` | fields filled on non-deal posts | ≤ 1 |

The final stage is a single look. If it fails, the loop stops and reports; it never tunes against holdout results.

## Rules (FIXED)

- One change per iteration. Edit only `extractors/claude.py` and new test files.
- Commit before every eval. Every results file is kept forever.
- Tune only on the dev split. Never open `data/holdout.jsonl`, `"split": "all"` results, or posts with `"split": "holdout"`.
- Stop when: dev passes (then the orchestrator runs final once), or 10 iterations are done, or 3 iterations in a row are reverted, or any API call fails.
- Never read `.env`. Never edit protected files (list in `check_success.py`).

## What is already known (FIXED)

- Starting point: prompt `de789943`. Dev mean is not yet measured at `--split dev`; iteration 0 measures it (3 runs, no code change).
- The holdout was scored once at `de789943` before this loop existed (`results/claude_20261024T231514Z.json`). That run is excluded from the loop's checks. Its specific failures are intentionally not listed here.
- Haiku 5.5 accepts a forced `tool_choice`, but a forced call skips thinking. It rejects non-default `temperature`, so outputs vary run to run; that's why each prompt is run 3 times.
- Opus 5.5 and Sonnet 5.5 reject forced `tool_choice` with a 400. Don't switch models.
- Tool `strict: true` makes the API enforce the schema (enums included), but requires `additionalProperties: false` and every property in `required`; optional fields become nullable. Transform the generated schema inside `claude.py`; never edit `schema.py`. Confirm against the API that strict mode works with this model and a forced tool choice before relying on it.

## Backlog (EDITABLE: re-rank after every iteration)

Re-ranked after iteration 2 (prompt `b0950301`, dev mean 0.9298; dev check fails only on mean, 0.930 vs 0.932). Rank weighs dev failures addressed against how general and low-risk the rule is. Done: enforce enums (iteration 1), property-manager requests are deals (iteration 2).

1. **Ambiguous city still goes in `city`; only `state` goes null** (s22 3/3, "ambiguous which Springfield"; s02 Portland 2/3). 5 failures, one general rule that matches the label convention (s02 label: city Portland, state null). Phrase it as "the city name as written", so it doesn't trigger state inference (s11).
2. **Timeline arithmetic** (s04 3/3, s25 2/3, s17 2/3, s13 1/3 = 8 failures; timeline is tied for weakest at 0.86). Add worked rules: subtract elapsed time from stated windows; "4 months" is over 90 days; an event "in 3 months" is the 31–90 bucket. Riskier, since several rules interact.
3. **Seller vs buyer** (s23 3/3: "lender-owned, $14M asking" is a sale; s07 3/3, also sell→buy = 6 failures). Read s07's raw output before writing the rule.
4. **State only when named** (s11 inferred IL from Chicago, now 3/3 runs). Watch it after any city change.
5. **State named in a licensing question still counts if it's the property's state** (s14 3/3).
6. **City from "outside X" / "near X"** (s08 3/3). The city is the named reference city. Could combine conceptually with #1, but keep it a separate change.

## Playbook (EDITABLE: how to iterate well)

- Fix errors before accuracy: one errored post costs every field on that post.
- Prefer a general rule plus one short counter-example over a list of special cases. Special cases overfit 24 posts.
- When a change helps one field and hurts another, check whether two rules now conflict before trying a third.
- Read the raw outputs in the results file (`records[*].raw`), not just the scores. The raw value shows what the model was thinking.
- Commit each results file before starting the next eval. `eval.py` counts an untracked results file as a dirty tree, and `check_success.py` ignores dirty runs (learned in iteration 0).
- Before relying on an API feature, probe it with one call on one dev post through `ClaudeExtractor` (load `.env` via `load_dotenv(path)` without printing anything). Cheaper than burning 3 evals on a 400 (learned in iteration 1).
- Strict tool schemas can fix more than the targeted error: in iteration 1 they also stopped a non-deal field fill. Recheck every failure after a structural change before re-ranking.
- Use exact counts, not the rounded percentages in the .md tables, for the keep/revert field-drop check. One post is 1/19 ≈ 5.3 points on key fields (19 deal posts) and 1/24 ≈ 4.2 on is_deal_request. A 2/3-of-a-post drop averaged over 3 runs usually means one post flipping in more runs, so look at which post it was before calling it a regression (learned in iteration 2).
