# Deal Requirements Board

A small prototype of the core of a "Deal Requirements Board" for cross-state real estate referrals. A broker posts a deal need in free text. The system extracts a structured requirement, filters brokers down to those **actively licensed in the deal's state**, and ranks them by fit with a plain-English reason for every point. When the post leaves out something essential, it asks one follow-up question instead of guessing.

Built as an internship portfolio project for License LinkUp ("Stay Compliant. Close Anywhere."). The emphasis is on measurement and compliance, not breadth.

```
POST /match  {"text": "Need a buyer's agent in Phoenix, AZ for an SFR up to $450k, closing in 30 days."}
```
```json
{
  "extractor": "regex",
  "requirement": {"is_deal_request": true, "state": "AZ", "city": "Phoenix", "property_type": "single_family",
                  "transaction_type": "buy", "deal_value_usd": 450000, "timeline": "within_30_days",
                  "notes": null, "missing_fields": []},
  "matches": [{"broker_id": "fx-04", "name": "Fictional Broker AZ-A", "source": "fictional",
               "license_number": "FICT-AZ-0001", "license_type": "Broker Individual", "score": 8,
               "reasons": ["+3 specializes in single_family", "+2 handles buy deals", "+2 serves Phoenix",
                           "+1 deal size $450,000 is within their $150,000–$5,000,000 range"]}],
  "excluded_for_licensing": 225,
  "follow_up_question": null
}
```

A second Phoenix single-family broker (`fx-05`) is in the data but isn't returned. Its status reads `Active`, but its license expired in 2025, so the gate removes it before scoring. A post without a state gets no matches at all:

```json
{"requirement": {"state": null, "city": "Portland", "...": "..."},
 "matches": [], "excluded_for_licensing": 226,
 "follow_up_question": "Which state is the Portland property in? We only match brokers licensed in that state."}
```

## How it works

| Stage | File | What it does |
|---|---|---|
| Schema | `deal_board/schema.py` | `DealRequirement`: deal or not, state (2-letter code), city, property type, transaction type, deal value, timeline bucket, notes. Every field except `is_deal_request` defaults to empty. The labeling rules live in the field descriptions. `missing_fields` is computed by code, never reported by the model. |
| Extract | `deal_board/extractors/claude.py` | One interface, `extract(text) -> DealRequirement`. The Claude extractor makes one forced tool call whose `input_schema` is the Pydantic schema, with `strict: true` so the API enforces enum values. The raw output must have exactly the schema's keys with strict JSON types. Bad output raises an error instead of being repaired. |
| | `deal_board/extractors/regex_baseline.py` | The regex baseline's typed loader. It runs the frozen file with one import remapped and checks that the output really is a `DealRequirement`. |
| | `deal_board/extractors/frozen/regex_baseline.py` | The regex baseline itself, byte-identical to tag `baseline-v1`. Never edited, not even its imports. |
| Gate | `deal_board/matcher.py` → `is_licensed` | A broker counts as licensed only if the license state matches, the status is exactly `Active`, and the expiration date is known and not past. A non-fictional row also needs a `trec_<date>` source no more than `MAX_SNAPSHOT_AGE_DAYS` (90) days old, because its status is frozen at download time. Anything unknown counts as not licensed. The date is passed in, never read from the clock. |
| Rank | `deal_board/matcher.py` → `match` | Points: property type +3, transaction type +2, market (city, or for Texas the city's county) +2, deal size in range +1. Each point carries a reason. Ordering is deterministic. Sales agents get the note "Referral fee must be paid through sponsoring broker". |
| Ask | `deal_board/followup.py` | A deterministic template, not model-generated. It asks about the highest-priority missing field, state first. It only repeats a city it already knows, so a post can't put words in its mouth. |
| Serve | `deal_board/api.py` | `POST /match`. Extractor, brokers, matcher and clock are injected dependencies. The clock is today's date in the easternmost US licensing time zone, so the expiration check can only be stricter, never more lenient. Errors: invalid model output → 422, upstream API failure → 503, both with a fixed message and no upstream text. `DEAL_EXTRACTOR=regex` forces the baseline; `DEAL_EXTRACTOR=claude` without a key, or an invalid value, gives a 503 instead of quietly serving regex; unset, it uses Claude when a key is set and otherwise falls back to regex with one warning. Post text is never logged; logs keep a keyed hash prefix and the length. |
| Config | `deal_board/config.py` | The only module that reads environment variables. The API key is wrapped as a secret straight away. `.env` is loaded only by the CLI entry points, never by library code or tests. |
| Measure | `deal_board/cli/eval.py`, `deal_board/scoring.py` | Per-field accuracy with 95% Wilson intervals, a separate hallucination column, and a paired exact McNemar comparison of two runs (`--compare`). Every run is saved to `results/` with its commit hash, model, prompt version and raw outputs, and, for runs since usage tracking was added, token usage. |
| | `tools/final_eval/` | The one-look final eval on holdout v3: `run.py` (refuses a second look, a dirty tree or a cached output), `validate.py` (checks a results file is a clean final look), `tables.py` (builds the result tables in this README), and the rules in `PROTOCOL.md`. |

**On compliance:** only an exact `Active` status plus an unexpired date counts as licensed. This is a conservative engineering choice, not a legal judgment. Probation statuses such as "Probation - Active" are excluded, and the system never guesses a state from a city. A wrong state would route a deal to brokers who can't legally take it, and asking one question is cheap.

## Results

The headline is holdout v3 ([`data/holdout_v3.jsonl`](data/holdout_v3.jsonl)): 25 posts, 19 deal requests and 6 non-deal. Sonnet wrote and labeled them, and Opus labeled the same texts blind ([`docs/holdout_v3_agreement.md`](docs/holdout_v3_agreement.md)). Both labelers are the same model family as the extractor, so they can share a blind spot with it. This table is the first and only look at holdout v3: each extractor was run once, from a clean `main`, following [`tools/final_eval/PROTOCOL.md`](tools/final_eval/PROTOCOL.md).

### Final holdout v3: `data/holdout_v3.jsonl`, 25 posts (19 deal, 6 non-deal)

| Field | Regex baseline | Claude `ac1a145b` | Δ (pts) | Hallucinated (regex / Claude) | p (exact McNemar) |
|---|---:|---:|---:|---:|---:|
| is_deal_request | 84% [65–94] | 100% [87–100] | +16 | – | 0.125 |
| state | 68% [46–85] | 89% [69–97] | +21 | 4 / 1 | 0.219 |
| city | 79% [57–91] | 89% [69–97] | +11 | 2 / 2 | 0.5 |
| property_type | 79% [57–91] | 95% [75–99] | +16 | 1 / 1 | 0.25 |
| transaction_type | 79% [57–91] | 100% [83–100] | +21 | 1 / 0 | 0.125 |
| deal_value_usd | 63% [41–81] | 95% [75–99] | +32 | 2 / 1 | 0.0703 |
| timeline | 100% [83–100] | 100% [83–100] | +0 | 0 / 0 | 1 |
| missing_fields | 32% [15–54] | 74% [51–88] | +42 | – | 0.00781 |
| **Mean of the 7 fields** | 0.789 | 0.955 | +17 | | |
| Fields guessed on deal posts | 10 | 5 | | | |
| Fields filled on non-deal posts | 10 | 0 | | | |
| Posts that errored | 0 | 0 | | | |

**Usage:** 25 API calls, 0 cache hits, 55,533 input / 5,979 output tokens, cost $0.0085 (prices as of 2026-10-06), latency 52.2 s total, 2.09 s mean.

**Provenance.** Regex: `results/regex_20261026T003559Z.json`, commit `452e5fd`, run `20261026T003559Z`. Claude: `results/claude_20261026T003610Z.json`, `claude-haiku-5-5`, prompt `ac1a145b`, commit `a2cc23d`, run `20261026T003610Z`. Both re-scored from their raw outputs against the current labels.

**First and only look** at `data/holdout_v3.jsonl`: each extractor was run on these posts exactly once, and no run was repeated after seeing a result.

### Validation

Both results files were checked with `tools/final_eval/validate.py`.

The regex run fails one check, `FAIL non_deal`: it filled 10 fields on four non-deal posts (v3-20, v3-22, v3-23, v3-24). That is a result about the baseline, and it is reported here, not fixed or rerun (PROTOCOL rule 6). The Claude run is `VALID`: 0 errors and 0 fields filled on non-deal posts.

<details>
<summary>validate.py output, regex baseline</summary>

```text
$ .venv/bin/python tools/final_eval/validate.py results/regex_20261026T003559Z.json
PASS extractor: extractor is 'regex'
PASS split: split is 'holdout_v3'
PASS datasets: path is 'data/holdout_v3.jsonl', expected 'data/holdout_v3.jsonl'
PASS records: 25 records, ids match the label file
PASS errors: 0 records with an error (–)
PASS complete: complete=True, aborted=None
PASS first_look: second_look=False, branch='main', protocol='tools/final_eval/PROTOCOL.md'
PASS labels: label file unchanged since the run
PASS provenance: fresh run: no rescore, no cache, every call fresh
PASS git_clean: git.dirty is False
PASS git_ancestor: commit 452e5fdf20de is in main's history
PASS prompt: prompt_version is None, expected null for regex
PASS model: model is None, expected null for regex
PASS raw_consistent: 0 records whose prediction/error doesn't match their raw output (–)
PASS scores: stored scores match a recomputation
PASS intervals: all 8 fields have accuracy inside its ci95
FAIL non_deal: 10 fields filled on non-deal posts (v3-20, v3-22, v3-23, v3-24)
INVALID (1 failed)
```

</details>

<details>
<summary>validate.py output, Claude</summary>

```text
$ .venv/bin/python tools/final_eval/validate.py results/claude_20261026T003610Z.json --expect-prompt ac1a145b --expect-model claude-haiku-5-5
PASS extractor: extractor is 'claude'
PASS split: split is 'holdout_v3'
PASS datasets: path is 'data/holdout_v3.jsonl', expected 'data/holdout_v3.jsonl'
PASS records: 25 records, ids match the label file
PASS errors: 0 records with an error (–)
PASS complete: complete=True, aborted=None
PASS first_look: second_look=False, branch='main', protocol='tools/final_eval/PROTOCOL.md'
PASS labels: label file unchanged since the run
PASS provenance: fresh run: no rescore, no cache, every call fresh
PASS git_clean: git.dirty is False
PASS git_ancestor: commit a2cc23dbb382 is in main's history
PASS prompt: prompt_version is 'ac1a145b', expected 'ac1a145b'
PASS model: model is 'claude-haiku-5-5'
PASS raw_consistent: 0 records whose prediction/error doesn't match their raw output (–)
PASS scores: stored scores match a recomputation
PASS intervals: all 8 fields have accuracy inside its ci95
PASS non_deal: 0 fields filled on non-deal posts (–)
VALID
```

</details>

### What the numbers support

The summary is the mean gain: +17 points, 0.789 for the regex baseline vs 0.955 for Claude. That is a measured difference on 25 posts, and the samples are small. The per-field 95% intervals overlap on every field.

Only `missing_fields` separates by exact McNemar at n=19 deal posts (p 0.00781). That p is uncorrected for testing 8 fields, and it would not survive a Bonferroni correction across them, so after correction no field separates. And `missing_fields` is derived from the other fields, so it isn't independent evidence. `deal_value_usd` is at p 0.0703, which is not significant. I don't claim significance for any other field or for the mean.

### Known-ambiguous labels (kept as shipped)

Three labels are arguable. I decided to keep them as shipped: no label was changed after the run.

- **v3-09 and v3-16, `city`.** The posts say "near Amarillo" and "outside Bend". The two labelers split on these, and the lead resolved both to null, because the property isn't in the named city. These are Claude's only two city misses. The regex also misses both.
- **v3-15, `is_deal_request`.** A request for a property-management company, labeled a deal. The schema contradicts itself here: one rule says a deal needs a specific transaction, another says property management gets a null transaction type, which implies it can still be a deal.

The line below is a secondary sensitivity check, with the two city labels left out. The headline stays the table above.

```text
Sensitivity (secondary): holdout v3 city accuracy with v3-09, v3-16 excluded
Sources: results/regex_20261026T003559Z.json, results/claude_20261026T003610Z.json, data/holdout_v3.jsonl
- all 25 posts: regex city 79% [57–91] (15/19 deal posts); Claude city 89% [69–97] (17/19 deal posts)
- 23 posts: regex city 88% [66–97] (15/17 deal posts); Claude city 100% [82–100] (17/17 deal posts)
- paired exact McNemar on the 17 remaining deal posts: p = 0.5 (Claude-only correct: v3-07, v3-08; regex-only correct: none)
```

### Baseline caveat

The frozen regex baseline takes the first state named in the text. On the red-team set ([`data/red_team.jsonl`](data/red_team.jsonl)), 16 of 30 red-team posts route to the wrong state on the regex path (pinned as `KNOWN_REGEX_STATE_LEAKS` in [`tests/test_red_team.py`](tests/test_red_team.py)). The licensing gate still never returns a broker who isn't actively licensed in the state it is given. A wrong state is an extraction error: the deal goes to brokers who are licensed, but in the wrong state ([`docs/agent-graphs/red-team.md`](docs/agent-graphs/red-team.md)). The baseline is a frozen comparison point, not a product path. The Claude extractor is the production path.

## Holdout v2: a second look during prompt development

Holdout v2 ([`data/holdout.jsonl`](data/holdout.jsonl), 15 posts) was scored once with the first prompt, before the improvement loop, and again with the final prompt at the end. So the final v2 number below is a second look made during prompt development, not an unseen test. The v3 table above is the final number.

On the 35 author-written posts ([`data/posts.jsonl`](data/posts.jsonl)) the baseline looks much better than on holdout v2, for example 97% on `deal_value_usd` and 100% on `timeline` ([`results/regex_20261024T224817Z.md`](results/regex_20261024T224817Z.md)), against 58% and 83% on holdout v2. I wrote the baseline's rules while looking at those posts, so they overstate it.

### Earlier holdout v2: `data/holdout.jsonl`, 15 posts (12 deal, 3 non-deal)

| Field | Regex baseline | Claude `ac1a145b` | Δ (pts) | Hallucinated (regex / Claude) | p (exact McNemar) |
|---|---:|---:|---:|---:|---:|
| is_deal_request | 93% [70–99] | 100% [80–100] | +7 | – | 1 |
| state | 75% [47–91] | 100% [76–100] | +25 | 0 / 0 | 0.25 |
| city | 75% [47–91] | 100% [76–100] | +25 | 1 / 0 | 0.25 |
| property_type | 75% [47–91] | 92% [65–99] | +17 | 0 / 1 | 0.625 |
| transaction_type | 75% [47–91] | 100% [76–100] | +25 | 1 / 0 | 0.25 |
| deal_value_usd | 58% [32–81] | 100% [76–100] | +42 | 1 / 0 | 0.0625 |
| timeline | 83% [55–95] | 75% [47–91] | -8 | 0 / 0 | 1 |
| missing_fields | 33% [14–61] | 92% [65–99] | +58 | – | 0.0156 |
| **Mean of the 7 fields** | 0.764 | 0.952 | +19 | | |
| Fields guessed on deal posts | 3 | 1 | | | |
| Fields filled on non-deal posts | 0 | 0 | | | |
| Posts that errored | 0 | 0 | | | |

**Usage:** not recorded (this results file predates usage tracking).

**Provenance.** Regex: `results/regex_20261024T224817Z.json`, commit `e6b0e42`, run `20261024T224817Z`. Claude: `results/claude_20261024T234359Z.json`, `claude-haiku-5-5`, prompt `ac1a145b`, commit `b13efb5`, run `20261024T234359Z`. Both re-scored from their raw outputs against the current labels.

**Holdout v2: not the final look.** These posts were scored during prompt development, so they are not an unseen test set; the final number is the holdout v3 table.

Both v2 files predate the per-post text and label hashes, so `tables.py` warns that it can't detect a post edited since those runs; they are scored by post id.

### How the prompt improved

The prompt was improved by an automated loop (`loop/`, `HANDOFF.md`) that tuned **only on the 24 dev posts**. It ran each candidate prompt 3 times, because Haiku 5.5 only accepts its default temperature, so outputs vary from run to run. It kept a change only if the dev mean rose, no field dropped by more than ~4 points (`HANDOFF.md`; a bit under one post on the 19 dev deal posts), and fills on non-deal posts didn't rise; iteration 3 met the first two and was reverted on the third. Iteration 2 was kept although state and city each dipped by less than one post. A success test in `loop/check_success.py` fixed the bar before the loop started and fails if the loop edits the scoring, the labels, or its own rules.

Prompt loop on the 24 dev posts (data/posts.jsonl split dev), 3 runs per prompt
Source: loop/state.json and the claude_*.json files it names in results/, re-scored from raw outputs against current labels
p: exact McNemar vs the previous kept prompt, discordant (post, field) cells over the 7 mean fields,
   pooled over the 3 run pairs (run k vs run k); repeated posts, and cells within one post moving together,
   make this p optimistic, not conservative

| Iteration | Prompt | Dev mean (3 runs) | Per-run min–max | vs | Better / worse cells | p | Decision |
|---|---|---:|---:|---|---:|---:|---|
| 0 | `de789943` | 0.885 | 0.881–0.890 | – | – | – | baseline |
| 1 | `a86a1ad8` | 0.926 | 0.926–0.926 | `de789943` | 25 / 8 | 0.00455 | kept |
| 2 | `b0950301` | 0.930 | 0.925–0.932 | `a86a1ad8` | 8 / 6 | 0.791 | kept |
| 3 | `b4f9c9ab` | 0.952 | 0.947–0.955 | `b0950301` | 10 / 1 | 0.0117 | reverted |
| 4 | `ac1a145b` | 0.945 | 0.940–0.955 | `b0950301` | 7 / 1 | 0.0703 | kept |

First prompt vs final prompt, one run each: results/claude_20261024T231514Z.json (de789943) vs results/claude_20261024T234359Z.json (ac1a145b)
- 11 untuned author posts (data/posts.jsonl split holdout): mean 0.957 -> 0.943; better / worse cells 2 / 3, p = 1; posts that changed: s12, s15, s30, s33
  post level (cells within a post move together, so this is the fairer test): posts with more / fewer correct fields 1 / 2, p = 1 (more: s30; fewer: s12, s33)
- holdout v2 (data/holdout.jsonl, 15 posts): mean 0.805 -> 0.952; better / worse cells 14 / 1, p = 0.000977; posts that changed: h01, h02, h06, h12
  post level (cells within a post move together, so this is the fairer test): posts with more / fewer correct fields 3 / 1, p = 0.625 (more: h01, h02, h12; fewer: h06)

What changed in each iteration:

- Iteration 0: the starting prompt, measured with no code change.
- Iteration 1: `strict: true` on the tool, so the API enforces enum values.
- Iteration 2: a request for a professional to handle specific properties counts as a deal; lenders and attorneys don't.
- Iteration 3: keep a named city when only its state is unclear. Reverted: it also filled a city on a hiring post.
- Iteration 4: the same rule, scoped to deal requests only. The dev gate passed.

The biggest gain was mechanical. Before strict mode, Claude sometimes invented a timeline value (`within_90_days`). Each invalid output failed the whole post, which cost every field on it.

Iterations 2 and 4 are within run-to-run noise on dev by the paired test: p = 0.791 and p = 0.0703, and that pooled p is optimistic. Among kept changes, only iteration 1 separates from the prompt before it; iteration 3 also separated (p = 0.0117) but was reverted. On the 11 untuned author posts (the holdout split of `data/posts.jsonl`), the final prompt `ac1a145b` scores slightly below the first prompt `de789943`, 0.957 -> 0.943, p = 1. That is no measurable change either way. First-prompt source: [`results/claude_20261024T231514Z.json`](results/claude_20261024T231514Z.json).

The gain on holdout v2 comes from three posts, and one got worse; at post level that is 3 / 1, p = 0.625 (the cell-level 14 / 1 overstates it, because cells within a post move together). h01 and h02 were invalid outputs under the first prompt, so every field counted as wrong; under the final prompt both are valid, most likely thanks to strict mode, though h02 still misses its timeline. h12, a hard money lender request, was called a deal and filled with five fields under the first prompt and is a non-deal under the final one. With only two runs on v2 I can't tell which iteration did that. h06's timeline was right under the first prompt and too short under the final one. The full reasoning for each iteration is in [`loop/LESSONS.md`](loop/LESSONS.md).

The first v2 run's failures were deliberately kept out of the loop's backlog, and the loop never read holdout results.

## Error analysis

Remaining failures of the final prompt on the development sets (`data/posts.jsonl` and holdout v2), grouped by cause, from one run. Source: [`results/claude_20261024T234359Z.json`](results/claude_20261024T234359Z.json).

1. **Timeline arithmetic: the main weakness** (holdout h02, h06, h08; dev s04, s17, s25). The model over-shortens: every timeline miss lands in a shorter bucket than the label. "Current lease is up in 4 months" (h02) came back as `within_30_days`. h08 and s04 both say "45-day ID clock started last week": about 38 days are left, so the label is `31_to_90_days`, and both came back `within_30_days`. h08 and s04 share that sentence verbatim, so they are one wording failing twice, not two independent failures. **Fix next:** stop asking the model to do arithmetic. Have it extract the duration phrase and number, and compute the bucket in code.
2. **Seller vs buyer** (s07, s15, s23, s33). Listing posts that seek buyers' agents ("seller wants $425k… cash buyers only") come back as `buy`. **Fix:** say in the schema that the transaction type is the poster's client's side, with one contrasting example.
3. **State inferred from an unambiguous city** (s11 Chicago → IL, s12 Scottsdale → AZ). The model knows the geography; the labeling rule forbids using it. s12 also gets a guessed `single_family` property type. This is a product question more than a model bug. One option is to let the model propose an inferred state flagged as unconfirmed, and have the follow-up question confirm it.
4. **"NNN" read as retail** (h08). Triple-net is a lease structure, not a property type. The schema description says so, but the association is strong.
5. **City after "outside" or "near"** (s08, "outside Savannah") and **state named inside a licensing question** (s14, "licensed in both NY and NJ" with NJ cities). s14's own label is in question: NJ can only be reached from the city names, which item 3's rule forbids (finding EM-8, left to the owner). The schema has no "near/outside X" rule, and the sets disagree: s08's label keeps Savannah and s15's ("near Boise") keeps Boise, while holdout v3 labels "near Amarillo" (v3-09) and "outside Bend" (v3-16) as no city.

Claude's 6 field failures on holdout v3 (11 lines including `missing_fields`) are listed in [`results/claude_20261026T003610Z.md`](results/claude_20261026T003610Z.md). None of them is a timeline miss: on v3, timeline accuracy on deal posts was 100% for both extractors (the regex baseline did fill a timeline on one non-deal post, v3-20).

The frozen regex baseline has known bugs, kept on purpose and pinned by `tests/test_baseline_golden.py`: `$9.50/sf` parses as 9, "flexible" matches "flex" (industrial), and "just closed the sale" marks a real 1031 request as non-deal.

## What's real and what's synthetic

| Data | Source | Notes |
|---|---|---|
| `data/posts.jsonl` (35 posts) | **Synthetic.** Written for this project in realistic broker shorthand (SFR, NNN, 1031, BOM, cap rate) | Covers hard cases on purpose: no state, cities in two states, several dollar amounts, vague timelines, non-deal posts. Split 24 dev / 11 holdout. |
| `data/holdout.jsonl` (15 posts, holdout v2) | **Synthetic**, written by a separate agent from `schema.py` | No brief was recorded, so I can't show what that agent saw, and `data/posts.jsonl` was already in the tree. It shares wording with `data/posts.jsonl`: h08 repeats s04's sentence "45-day ID clock started last week" verbatim, and h13~s06, h14~s13 and h05~s05 share shorter phrases. Treat it as a development set seen twice, not a blind one. Holdout v3 is the stronger independent set. |
| `data/holdout_v3.jsonl` (25 posts) | **Synthetic**, written and labeled by Claude Sonnet from a recorded blind brief, then labeled again blind by Claude Opus | 19 deal requests, 6 non-deals. The record says the labelers saw only `schema.py`, the record shape (the first line's keys of `data/holdout.jsonl`) and a variety checklist; that is recorded, not verified. Both labelers are the same model family as the extractor, so they may share blind spots. A different-model holdout remains planned. Agreement and adjudication: [`docs/holdout_v3_agreement.md`](docs/holdout_v3_agreement.md). |
| `data/red_team.jsonl` (30 posts) | **Synthetic, adversarial.** Written by an attacker agent that read the schema, the matcher and the regex baseline | Each post tries to route a deal to the wrong state: two states in one post, a license named elsewhere, state-code collisions, implied cities, unicode tricks, and prompt injection inside the post. Graph: [`docs/agent-graphs/red-team.md`](docs/agent-graphs/red-team.md). |
| `data/brokers.csv`, TREC rows (200) | **Real public license records** from the Texas Real Estate Commission licensee download (Oct 20, 2026) | **Names removed, not anonymized.** License numbers are kept, and they are public lookups. Stratified sample: 150 valid, 50 inactive, expired, or "Active" with a placeholder 1990/1991 expiration. Specialty, market and deal-size fields are blank, because TREC doesn't publish them and inventing them for real people would be fabrication. County comes from the license holder's address, not necessarily where they work. |
| `data/brokers.csv`, fictional rows (26, from `data/fictional_brokers.csv`) | **Fictional**, every name prefixed "Fictional", every license number `FICT-` | Includes deliberate traps: an inactive perfect fit, Active but expired, Active with no expiration, suspended, wrong state, a sales agent. |

The raw TREC file stays in `data/raw/`, which is gitignored and never committed. No private groups or sites that prohibit scraping were used.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                  # 1522 passed, 1 skipped in about 42 s; offline, no API key needed
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy --strict
```

Evaluation (Claude runs need a key in `.env`; see `.env.example`):

```bash
.venv/bin/deal-eval --extractor regex                        # baseline on posts.jsonl and holdout v2
.venv/bin/deal-eval --extractor claude --split dev           # tuning posts only
.venv/bin/deal-eval --extractor claude --split dev --repeats 3   # same, run 3 times
.venv/bin/deal-eval --rescore results/<file>.json            # re-score saved outputs, no API calls
.venv/bin/deal-eval --compare results/<a>.json results/<b>.json   # paired per-post comparison, writes nothing
```

The final eval on holdout v3 is a different tool, `tools/final_eval/run.py`. It is a one-look run, done once per extractor by the orchestrator and already done. It refuses to run again, and its rules are in [`tools/final_eval/PROTOCOL.md`](tools/final_eval/PROTOCOL.md). `tools/final_eval/validate.py` and `tools/final_eval/tables.py` only read the saved results and are safe to run.

API:

```bash
DEAL_EXTRACTOR=regex .venv/bin/uvicorn deal_board.api:app --reload        # the API reads ANTHROPIC_API_KEY from the environment only; it never loads .env
curl -s localhost:8000/match -H 'content-type: application/json' -d '{"text": "Need a listing agent in Columbus, Ohio for a 3bd ranch, about $275k"}'
```

Rebuilding the broker sample from a fresh TREC download: `.venv/bin/deal-build-brokers data/raw/<file>.csv`. It uses a fixed seed, so the same input gives the same 200 rows.

## Engineering notes

- **Evaluation first.** Labeled data and scoring rules existed before any prompt was written. `results/` is a permanent log; no run is ever deleted.
- **Provenance.** Every results file records the commit, a dirty-tree flag, the model and `PROMPT_VERSION`, a hash of the prompt and tool schema, and, since usage tracking was added, token usage. The loop rejects results from a dirty tree.
- **Tests that defend the claims.** The compliance test runs every labeled post against every broker, plus generated variants (each non-Active status, expired yesterday, no expiration, wrong state). It fails if an unlicensed broker is ever returned. Every Claude test uses a fake client.
- **Mutation testing.** Deliberate bugs were applied to the gate and the match path one at a time, to see which ones the suite misses ([`review/mutants/REPORT.md`](review/mutants/REPORT.md)). That pass did not converge: 45 of 160 mutants survived, 13 of them high severity, after its 4 rounds. The gaps were mostly around the gate, such as the production clock and the broker builder. After the fixes, a fresh reviewer's 39 new mutants were all killed ([`review/fixes2/POSTREVIEW.md`](review/fixes2/POSTREVIEW.md)). The tests are in `tests/test_api_mutants.py`, `tests/test_brokers_mutants.py`, `tests/test_build_brokers.py`, `tests/test_extractor_mutants.py`, `tests/test_matcher_mutants.py` and `tests/test_postfix_mutants.py`.
- **Reviews.** Separate read-only review passes covered eval quality, compliance and security, and API correctness. Each finding was verified before it was fixed. Findings and logs are in [`review/`](review/).
- **Built with parallel agent sessions.** The matcher and the API were built at the same time against one fixed interface, each in its own worktree with an adversarial review step. The red team, holdout v3, packaging and this docs pass each have a graph in [`docs/agent-graphs/`](docs/agent-graphs/).

## Limitations and next steps

- **Small samples.** Holdout v3 has 19 deal posts, so one post moves a field by several points and the intervals are wide: a 100% timeline score there has a 95% Wilson interval of 83–100%.
- **Timeline arithmetic.** The model over-shortens timelines. Next: have it extract the duration phrase and compute the bucket in code.
- **Seller vs buyer.** Listing posts that seek buyers' agents still come back as `buy`. Next: one schema sentence and a contrasting example.
- **Schema gaps found by holdout v3.** There is no "near/outside X" rule for city, and no written rule that a vague amount ("mid six figures") is null. Both labelers applied conventions the schema never states ([`docs/holdout_v3_agreement.md`](docs/holdout_v3_agreement.md)).
- **The TREC sample goes stale.** The gate rejects TREC rows whose snapshot is more than 90 days old, so all 200 real rows stop matching 90 days after the 2026-10-20 download. Next: refresh the download with `deal-build-brokers`.
- **CI has never run.** The workflow is defined in `.github/workflows/ci.yml` (ruff, format check, mypy, pytest), but the repo has no remote, so it has never been triggered.
- **A different-model holdout.** Every set so far was written and labeled by Claude models. Next: a holdout written and labeled by a non-Claude model, with a human spot check of the labels.
