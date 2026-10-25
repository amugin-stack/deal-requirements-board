# Review loop log

Each round: two read-only reviewer subagents (eval_method, code_quality) in parallel, then one adversarial
verifier subagent per candidate with its own `python -I` script in the session scratchpad. Verifier scripts
are embedded verbatim in `findings.json`.

## Round 1

- reviewers: eval_method (9 candidates, plus the README numbers audit), code_quality (8 candidates)
- candidates: 17 — EM-1..EM-9, CQ-1..CQ-8
- confirmed: 13 — EM-1, EM-2, EM-3, EM-5, EM-6, EM-7, EM-8, CQ-2, CQ-3, CQ-4, CQ-5, CQ-7, CQ-8
- rejected: 4 — EM-4 (already disclosed, not misleading), EM-9 (documented derived metric, not double-counted), CQ-1 (duplicate of EM-5), CQ-6 (unreachable: argparse limits --split)
- severity changes by verifiers: EM-1 high->medium, EM-2/EM-3 medium->low, CQ-5 medium->low; several claims narrowed (see verifier_note)
- README audit: all 27 numeric cells match results/*.json; prose mismatches: keep rule (EM-2), "500 tests" vs 837
- new_confirmed: 13

## Round 2

- reviewers were given the round 1 confirmed and rejected lists and told to try new surfaces (label quality, docs vs results, cost/usage, hallucination denominators, RawCache, load_run, schema validators, test design)
- candidates: 9 — EM-21..EM-25, CQ-21..CQ-24
- confirmed: 9 — EM-21, EM-22, EM-23, EM-24, EM-25, CQ-21, CQ-22, CQ-23, CQ-24
- rejected: 0
- severity changes by verifiers: CQ-21 medium->low (truncation implausible at max_tokens 1024)
- checked clean this round: holdout_v3 kappa/agreement numbers recomputed from labels; cost table price and cache-hit exclusion; LESSONS.md/state.json vs results; README error-analysis post lists; compare pairing with errored records; RawCache atomic put; save() exclusive create
- new_confirmed: 9

## Round 3

- reviewers given all 26 prior findings (confirmed and rejected) and pointed at untried surfaces: holdout provenance and prompt-change lineage, gate thresholds' timing, single-look count, README compliance numbers, cross-set label conventions, guard/PROTECTED design, golden and compliance tests via mutation
- candidates: 2 — EM-31, CQ-31
- confirmed: 2 — EM-31, CQ-31
- rejected: 0
- severity changes by verifiers: EM-31 medium->low (overlapping posts don't drive the gain), CQ-31 high->medium (needs a deliberately adversarial agent)
- checked clean this round: DEV_MIN_MEAN fixed at loop-start before any dev run; exactly one split=all Claude run after loop-start; README '44 tests fail' and 'excluded_for_licensing 225/226' reproduce; deal_value/property_type/is_deal conventions agree between posts.jsonl and holdout.jsonl; fixed_sections guard fails closed; 15 of 19 matcher/followup mutants killed (4 equivalent)
- new_confirmed: 2
