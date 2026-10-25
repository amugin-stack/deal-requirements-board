# Deal Requirements Board

A small, rigorous prototype of the core of a "Deal Requirements Board" for cross-state real estate referrals. A broker posts a deal need in free text. The system extracts a structured requirement, filters brokers down to those **actively licensed in the deal's state**, and ranks them by fit with a plain-English reason for every point. When the post leaves out something essential, it asks one follow-up question instead of guessing.

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
| Schema | `deal_board/schema.py` | `DealRequirement`: state (2-letter code), city, property type, transaction type, deal value, timeline bucket, notes. Every field defaults to empty. The labeling rules live in the field descriptions. `missing_fields` is computed by code, never reported by the model. |
| Extract | `deal_board/extractors/regex_baseline.py`, `deal_board/extractors/claude.py` | One interface, `extract(text) -> DealRequirement`. The regex baseline is frozen at tag `baseline-v1`. The Claude extractor makes one forced tool call whose `input_schema` is the Pydantic schema, with `strict: true` so the API enforces enum values. Bad output raises an error instead of being repaired. |
| Gate | `deal_board/matcher.py` → `is_licensed` | A broker counts as licensed only if the license state matches, the status is exactly `Active`, and the expiration date is known and not past. Anything unknown counts as not licensed. The date is passed in, never read from the clock. |
| Rank | `deal_board/matcher.py` → `match` | Points: property type +3, transaction type +2, market (city, or for Texas the city's county) +2, deal size in range +1. Each point carries a reason. Ordering is deterministic. Sales agents get the note "Referral fee must be paid through sponsoring broker". |
| Ask | `deal_board/followup.py` | A deterministic template, not model-generated. It asks about the highest-priority missing field, state first. |
| Serve | `deal_board/api.py` | `POST /match`. Extractor, brokers, matcher and clock are injected dependencies. Errors: invalid model output → 422, upstream API failure → 503 with no upstream text. Post text is never logged; logs keep a hash prefix and the length. |
| Measure | `deal_board/cli/eval.py`, `deal_board/scoring.py` | Per-field accuracy, with a separate hallucination column, on each labeled set. Every run is saved to `results/` with its commit hash, model, prompt version and raw outputs. |

**On compliance:** only an exact `Active` status plus an unexpired date counts as licensed. This is a conservative engineering choice, not a legal judgment. Probation statuses such as "Probation - Active" are excluded, and the system never guesses a state from a city. A wrong state would route a deal to brokers who can't legally take it, and asking one question is cheap.

## Results

Scored on an **independent holdout** of 15 posts (12 deal requests, 3 non-deal). A separate agent wrote it, seeing only `schema.py`, never the extractors or the other posts. Field rows count deal posts only, so one post moves a field by about 8 points.

| Field (holdout) | Regex baseline | Claude, first prompt | Claude, final prompt |
|---|---:|---:|---:|
| is_deal_request | 93% | 80% | **100%** |
| state | 75% | 83% | **100%** |
| city | 75% | 83% | **100%** |
| property_type | 75% | 75% | **92%** |
| transaction_type | 75% | 83% | **100%** |
| deal_value_usd | 58% | 83% | **100%** |
| timeline | 83% | 75% | 75% |
| missing_fields (exact set) | 33% | 75% | **92%** |
| **Mean of the 7 fields** | 0.764 | 0.805 | **0.952** |
| Fields guessed on deal posts | 3 | 1 | 1 |
| Fields filled on non-deal posts | 0 | 5 | **0** |
| Posts that errored | 0 | 2 | **0** |

Claude is `claude-haiku-5-5`. The first prompt is `de789943`; the final prompt is `ac1a145b`. Sources: [`results/regex_20261024T224817Z.json`](results/regex_20261024T224817Z.json), [`results/claude_20261024T231514Z.json`](results/claude_20261024T231514Z.json), [`results/claude_20261024T234359Z.json`](results/claude_20261024T234359Z.json). All three were run from clean commits.

On the 35 author-written posts, the final prompt scores a 0.941 mean, tied with the baseline at 0.941. That tie is expected. I wrote the baseline's rules while looking at those posts, so they overstate it: the baseline loses 18 points from that set (0.941) to the independent holdout (0.764). That gap is why the holdout is the headline number.

### How the prompt improved

The prompt was improved by an automated loop (`loop/`, `HANDOFF.md`) that tuned **only on the 24 dev posts**. It ran each candidate prompt 3 times, because Haiku 5.5 runs at a fixed temperature, and kept a change only if the mean rose and no field regressed. A success test in `loop/check_success.py` fixed the bar before the loop started and fails if the loop edits the scoring, the labels, or its own rules.

| Iteration | Change | Dev mean (3 runs) | Decision |
|---|---|---:|---|
| 0 | Starting prompt `de789943` | 0.885 | — |
| 1 | `strict: true` on the tool, so the API enforces enum values | 0.926 | kept |
| 2 | A request for a professional to handle specific properties counts as a deal; lenders and attorneys don't | 0.930 | kept |
| 3 | Keep a named city when only its state is unclear | 0.952 | **reverted**: also filled a city on a hiring post |
| 4 | Same rule, scoped to deal requests only | 0.945 | kept; dev gate passed |

The biggest gain was mechanical. Before strict mode, Claude sometimes invented a timeline value (`within_90_days`). Each invalid output failed the whole post, which cost every field on it. The full reasoning for each iteration is in [`loop/LESSONS.md`](loop/LESSONS.md).

**Disclosure:** the holdout was scored once with the first prompt, before the loop existed, and once more at the end. The final number is its second look. Its failures from the first run were deliberately kept out of the loop's backlog, and the loop never read holdout results.

## Error analysis

Remaining failures of the final prompt, grouped by cause:

1. **Timeline arithmetic: the main weakness** (holdout h02, h06, h08; dev s04, s17, s25). The model buckets durations wrong. For example, "lease is up in 4 months" came back as `within_30_days`, and it doesn't subtract time already elapsed in a 45-day window. **Fix next:** stop asking the model to do arithmetic. Have it extract the duration phrase and number, and compute the bucket in code.
2. **Seller vs buyer** (s07, s15, s23, s33). Listing posts that seek buyers' agents ("seller wants $425k… cash buyers only") come back as `buy`. **Fix:** say in the schema that the transaction type is the poster's client's side, with one contrasting example.
3. **State inferred from an unambiguous city** (s11 Chicago → IL, s12 Scottsdale → AZ). The model knows the geography; the labeling rule forbids using it. This is a product question more than a model bug. One option is to let the model propose an inferred state flagged as unconfirmed, and have the follow-up question confirm it.
4. **"NNN" read as retail** (h08). Triple-net is a lease structure, not a property type. The schema description says so, but the association is strong.
5. **City after "outside" or "near"** (s08, "outside Savannah") and **state named inside a licensing question** (s14, "licensed in both NY and NJ" with NJ cities).

The frozen regex baseline has known bugs, kept on purpose and pinned by `tests/test_baseline_golden.py`: `$9.50/sf` parses as 9, "flexible" matches "flex" (industrial), and "just closed the sale" marks a real 1031 request as non-deal.

## What's real and what's synthetic

| Data | Source | Notes |
|---|---|---|
| `data/posts.jsonl` (35 posts) | **Synthetic.** Written for this project in realistic broker shorthand (SFR, NNN, 1031, BOM, cap rate) | Covers hard cases on purpose: no state, cities in two states, several dollar amounts, vague timelines, non-deal posts. Split 24 dev / 11 holdout. |
| `data/holdout.jsonl` (15 posts) | **Synthetic**, written blind by a separate agent from `schema.py` alone | Same model family as the author, so it may share blind spots. A different-model holdout is planned. |
| `data/brokers.csv`, TREC rows (200) | **Real public license records** from the Texas Real Estate Commission licensee download (Oct 20, 2026) | **Names removed, not anonymized.** License numbers are kept, and they are public lookups. Stratified sample: 150 valid, 50 inactive, expired, or "Active" with a placeholder 1990/1991 expiration. Specialty, market and deal-size fields are blank, because TREC doesn't publish them and inventing them for real people would be fabrication. County comes from the license holder's address, not necessarily where they work. |
| `data/brokers.csv`, fictional rows (26) | **Fictional**, every name prefixed "Fictional", every license number `FICT-` | Includes deliberate traps: an inactive perfect fit, Active but expired, Active with no expiration, suspended, wrong state, a sales agent. |

The raw TREC file stays in `data/raw/`, which is gitignored and never committed. No private groups or sites that prohibit scraping were used.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                    # 500 tests, offline, no API key needed
```

Evaluation (Claude runs need a key in `.env`; see `.env.example`):

```bash
.venv/bin/deal-eval --extractor regex   # baseline on both sets
.venv/bin/deal-eval --extractor claude --split dev   # tuning posts only
.venv/bin/deal-eval --rescore results/<file>.json   # re-score saved outputs, no API calls
```

API:

```bash
DEAL_EXTRACTOR=regex .venv/bin/uvicorn deal_board.api:app --reload        # omit DEAL_EXTRACTOR to use Claude when a key is set
curl -s localhost:8000/match -H 'content-type: application/json' -d '{"text": "Need a listing agent in Columbus, Ohio for a 3bd ranch, about $275k"}'
```

Rebuilding the broker sample from a fresh TREC download: `.venv/bin/deal-build-brokers data/raw/<file>.csv`. It uses a fixed seed, so the same input gives the same 200 rows.

## Engineering notes

- **Evaluation first.** Labeled data and scoring rules existed before any prompt was written. `results/` is a permanent log; no run is ever deleted.
- **Provenance.** Every results file records the commit, a dirty-tree flag, the model and `PROMPT_VERSION`, a hash of the prompt and tool schema. The loop rejects results from a dirty tree.
- **Tests that defend the claims.** The compliance test runs every labeled post against every broker, plus generated variants (each non-Active status, expired yesterday, no expiration, wrong state). It fails if an unlicensed broker is ever returned. Deliberately breaking the expiration check made 44 of these tests fail. Every Claude test uses a fake client.
- **Built with parallel agent sessions.** The matcher and the API were built at the same time against one fixed interface, each in its own worktree with an adversarial review step. Graphs are in [`docs/agent-graphs/`](docs/agent-graphs/).

## Limitations and next steps

- **Small samples.** 12 holdout deal posts means a perfect 12/12 has a 95% Wilson interval of about 76–100%. Next: report intervals and a per-post paired comparison.
- **A resumable eval cache**, token and cost tracking, and a `--repeats` flag.
- **Packaging.** A `pyproject.toml` package instead of `sys.path` edits in scripts, plus `ruff`, `mypy --strict` and CI.
- **A final review before v1.0.** A different-model holdout with a second annotator, a compliance red team (including prompt injection inside posts), and an architecture review whose findings are each verified before fixing.
